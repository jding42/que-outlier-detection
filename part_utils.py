'''
Utilities functions
'''
import torch
import numpy
import numpy as np
import os
import os.path as osp
import pickle
import argparse
from scipy.stats import ortho_group
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd

import pdb

#parse the configs from config file

def read_config():
    with open('config', 'r') as file:
        lines = file.readlines()
    
    name2config = {}
    for line in lines:
        
        if line[0] == '#' or '=' not in line:
            continue
        line_l = line.split('=')
        name2config[line_l[0].strip()] = line_l[1].strip()
    m = name2config
    if 'kahip_dir' not in m or 'data_dir' not in m or 'glove_dir' not in m or 'sift_dir' not in m:
        raise Exception('Config must have kahip_dir, data_dir, glove_dir, and sift_dir')
    return name2config

name2config = read_config()

device = 'cuda' if torch.cuda.is_available() else 'cpu'
kahip_dir = name2config['kahip_dir'] 
graph_file = 'knn.graph'
data_dir = name2config['data_dir'] 

parts_path = osp.join(data_dir, 'partition', '')
dsnode_path = osp.join(data_dir, 'train_dsnode')

glove_dir = name2config['glove_dir'] 
sift_dir = name2config['sift_dir'] 

#starter numbers
N_CLUSTERS = 256 #16
N_HIDDEN = 512
#for reference, this is 128 for sift, 784 for mnist, and 100 for glove
N_INPUT = 128 

'''                                                        
One unified parse_args to encure consistency across different components.
Returns opt.
'''
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--n_clusters', default=N_CLUSTERS, type=int, help='number of cluseters' )
    parser.add_argument('--kahip_config', default='strong', help='fast, eco, or strong' )
    parser.add_argument('--parts_path_root', default=parts_path, help='path root to partition')
    parser.add_argument('--dsnode_path', default=dsnode_path, help='path to datanode dsnode for training')
    parser.add_argument('--k', default=10, type=int, help='number of neighbors')
    parser.add_argument('--nn_mult', default=1, type=int, help='multiplier for opt.k to create distribution of bins of nearest neighbors during training')
    parser.add_argument('--data_dir', default=data_dir, help='data dir')
    parser.add_argument('--graph_file', default=graph_file, help='file to store knn graph')

    parser.add_argument('--glove', default=True, help='whether using glove data')
    parser.add_argument('--glove_c', default=False, help='whether using glove data')
    parser.add_argument('--sift_c', default=False, help='whether using glove data')
    parser.add_argument('--sift', default=False, help='whether using SIFT data')
    parser.add_argument('--fast_kmeans', default=True, help='whether using fast kmeans, non-sklearn')
    parser.add_argument('--itq', default=False, help='whether using ITQ solver')
    parser.add_argument('--pca', default=False, help='whether using PCA solver')
    parser.add_argument('--rp', default=False, help='whether using random projection solver')
    parser.add_argument('--kmeans_use_kahip_height', default=-2, type=int, help='height if kmeans using kahip height, i.e. for combining kahip+kmeans methods')
    parser.add_argument('--compute_gt_nn', default=False, help='whether to compute ground-truth for dataset points. Ground truth partitions instead of learned, ie if everything were partitioned by kahip')
    
    #meta and more hyperparameters
    parser.add_argument('--write_res', default=True, help='whether to write acc and probe count results for kmeans')
    parser.add_argument('--normalize_data', default=False, help='whether to normalize input data')
    #parser.add_argument('--normalize_feature', default=True, help='whether to scale features')
    parser.add_argument('--max_bin_count', default=30, type=int, help='max bin count for kmeans') #default=160
    parser.add_argument('--acc_thresh', default=0.97, type=float, help='acc threshold for kmeans')
    parser.add_argument('--n_repeat_km', default=3, type=int, help='number of experimental repeats for kmeans')
    
    #params for training
    parser.add_argument('--n_input', default=N_INPUT, type=int, help='dimension of neural net input')
    parser.add_argument('--n_hidden', default=N_HIDDEN, type=int, help='hidden dimension')
    parser.add_argument('--n_class', default=N_CLUSTERS, type=int, help='number of classes for trainig')
    parser.add_argument('--n_epochs', default=1, type=int, help='number of epochs for trainig') #35
    parser.add_argument('--lr', default=0.0008, type=float, help='learning rate')    

    opt = parser.parse_args()
    
    if opt.glove:    
        opt.n_input = 100
    elif opt.glove_c:
        opt.n_input = 100        
    elif opt.sift or opt.sift_c:
        opt.n_input = 128
    else:
        opt.n_input = 784 #for mnist
        
    if (opt.glove or opt.glove_c) and not opt.normalize_data:
        print('GloVe data must be normalized! Setting normalize_data to True...')
        opt.normalize_data = True
        
    if opt.glove and opt.sift:        
        raise Exception('Must choose only one of opt.glove and opt.sift!')
    
    if not opt.fast_kmeans^opt.itq:
        #raise Exception('Must choose only one of opt.fast_kmeans and opt.itq!')
        print('NOTE: fast_kmeans and itq options share the same value')
        
    if not opt.fast_kmeans:
        print('NOTE: fast_kmeans not enabled')
        
    return opt 

class NestedList:
    def __init__(self):
        self.master = {}

    def add_list(self, l, idx):
        if not isinstance(l, list):
            raise Exception('Must add list to ListWrapper!')
        self.master[idx] = l
        
    def get_list(self, idx):
        return self.master[idx]
'''
l2 normalize along last dim
Input: torch tensor.
'''
def normalize(vec):
    norm = vec.norm(p=2, dim=-1, keepdim=True)
    return vec/norm

def normalize_np(vec):
    norm = numpy.linalg.norm(vec, axis=-1, keepdims=True)    
    return vec/norm

'''
Cross polytope LSH
To find the part, Random rotation followed by picking the nearest spherical
lattice point after normalization, ie argmax, not up to sign.
Input:
-M: projection matrix
-n_clusters, must be divisible by 2.
'''
def polytope_lsh(X, n_clusters):
    #random orthogonal rotation
    
    #M = torch.randn(X.size(-1), proj_dim)
    M = torch.from_numpy(ortho_group.rvs(X.size(-1)))
    proj_dim = n_clusters / 2
    M = M[:, :proj_dim]
    X = torch.mm(X, M)    
    #X = X[:, :proj_dim]
    
    max_idx = torch.argmax(X.abs(), dim=-1) #check dim!
    max_entries = torch.gather(X, dim=-1, index=max_idx)
    #now in range e.g. [-8, 8]
    max_idx[max_entries<0] = -max_idx[max_entries<0]
    max_idx += proj_dim
    return M, max_idx.view(-1)

'''
get ranking using cross polytope info.
Input:
-q: query input, 2D tensor
-M: projection mx. 2D tensor. d x n_total_clusters/2
'''
def polytope_rank(q, M, n_bins):  
    q = torch.mm(q, M)
    n_queries, d = q.size(0), q.size(0)
    q = q.view(-1)
    bases = torch.eye(d, device=device)
    bases = torch.cat((bases, -bases), dim=0)
    bases_exp = bases.unsqueeze(0).expand(n_queries, 2*d, d)
    #multiply in last dimension
    idx = torch.topk((bases_exp*q).sum(-1), k=n_bins, dim=-1)
    return idx    

'''
Compute histograms of distances to the mth neighbor. Useful for e.g.
after catalyzer processing.
Input:
-X: data
-q: queries
-m: the mth neighbor to take distance to.
'''
def plot_dist_hist(X, q, m, data_name):    
    dist = l2_dist(q, X)    
    dist, ranks = torch.topk(dist, k=m, dim=-1, largest=False)    
    dist = dist / dist[:, 0].unsqueeze(-1)
    #first look at the mean and median of distances
    mth_dist = dist[:, m-1]
    plt.hist(mth_dist.cpu().numpy(), bins=100, label=str(m)+'th neighbor')
    plt.xlabel('distance')
    plt.ylabel('count')
    plt.xlim(0, 4)
    plt.ylim(0, 140)
    plt.title('Dist to {}^th nearest neighbor'.format(m))
    plt.grid(True)
    fig_path = osp.join(data_dir, '{}_dist_{}_hist.jpg'.format(data_name, m))
    plt.savefig(fig_path)
    print('fig saved {}'.format(fig_path))
    #pdb.set_trace()
    return mth_dist, plt
'''
Plot distance scatter plot, *up to* m^th neighbor, normalized by nearest neighbor dist.
'''
def plot_dist_hist_upto(X, q, m, data_name): 
    dist = l2_dist(q, X)    
    dist, ranks = torch.topk(dist, k=m, dim=-1, largest=False)    
    dist = dist / dist[:, 0].unsqueeze(-1)
    #first look at the mean and median of distances
    m_dist = dist[:, :m]
    m_dist = m_dist.mean(0)

    df = pd.DataFrame({'k':list(range(m)), 'dist':m_dist.cpu().numpy()})
    fig = sns.scatterplot(x='k', y='dist', data=df, label=data_name)
    fig.figure.legend()
    fig.set_title('{}: distance wrt k up to {}'.format(data_name, m))
    fig_path = osp.join(data_dir, '{}_dist_upto{}.jpg'.format(data_name, m))
    fig.figure.savefig(fig_path)
    print('figure saved under {}'.format(fig_path))
    
    
    '''
    plt.hist(mth_dist.cpu().numpy(), bins=100, label=str(m)+'th neighbor')
    plt.xlabel('distance')
    plt.ylabel('count')
    plt.xlim(0, 4)
    plt.ylim(0, 140)
    plt.title('Dist to {}^th nearest neighbor'.format(m))
    plt.grid(True)
    fig_path = osp.join(data_dir, '{}_dist_{}_hist.jpg'.format(data_name, m))
    plt.savefig(fig_path)
    print('fig saved {}'.format(fig_path))
    #pdb.set_trace()
    return mth_dist, plt
    '''
    
'''
Type can be query, train, and or answers.
'''
def load_data_dep(type='query'):
    if type == 'query':
        return torch.from_numpy(np.load(osp.join(data_dir, 'queries_unnorm.npy')))
    elif type == 'answers':
        #answers are NN of the query points
        return torch.from_numpy(np.load(osp.join(data_dir, 'answers_unnorm.npy')))
    elif type == 'train':
        return torch.from_numpy(np.load(osp.join(data_dir, 'dataset_unnorm.npy')))
    else:
        raise Exception('Unsupported data type')

'''
All data are normalized.
glove_dir : '~/partition/glove-100-angular/normalized'
'''
def load_glove_data(type='query'):
    if type == 'query':
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_queries.npy')))
    elif type == 'answers':
        #answers are NN of the query points
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_answers.npy')))
    elif type == 'train':
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_dataset.npy')))
    else:
        raise Exception('Unsupported data type')

'''
catalyzer'd glove data
'''
def load_glove_c_data(type='query'):
    if type == 'query':
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_c0.08_queries.npy')))
    elif type == 'answers':
        #answers are NN of the query points
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_answers.npy')))
    elif type == 'train':
        return torch.from_numpy(np.load(osp.join(data_dir, 'glove_c0.08_dataset.npy')))
    else:
        raise Exception('Unsupported data type')

def load_sift_c_data(type='query'):
    if type == 'query':
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_c_queries.npy')))
    elif type == 'answers':
        #answers are NN of the query points
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_answers.npy')))
    elif type == 'train':
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_c_dataset.npy')))
    else:
        raise Exception('Unsupported data type')

    
'''
All data are normalized.
glove_dir : '~/partition/glove-100-angular/normalized'
'''
def load_sift_data(type='query'):
    if type == 'query':
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_queries_unnorm.npy')))
    elif type == 'answers':
        #answers are NN of the query points
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_answers_unnorm.npy')))
    elif type == 'train':
        return torch.from_numpy(np.load(osp.join(data_dir, 'sift_dataset_unnorm.npy')))
    else:
        raise Exception('Unsupported data type')

'''
Glove data according
Input:
-n_parts: number of parts.
'''
def glove_top_parts_path(n_parts):
    if n_parts not in [2, 4, 8, 16, 32, 64, 128, 256, 512]:
        raise Exception('Glove partitioning has not been precomputed for {} parts.'.format(n_parts))
    strength = 'strong' #'eco' if n_parts in [128, 256] else 'strong'
    glove_top_parts_path = osp.join(glove_dir, 'partition_{}_{}'.format(n_parts, strength), 'partition.txt')
    if n_parts == 16:
        glove_top_parts_path = '/home/yihdong/partition/data/partition/16strongglove0ht1'
    return glove_top_parts_path

'''
SIFT partitioning.
Input:
-n_parts: number of parts.
'''
def sift_top_parts_path(n_parts):
    if n_parts not in [2, 4, 8, 16, 32, 64, 128, 256]:
        raise Exception('SIFT partitioning has not been precomputed for {} parts.'.format(n_parts))
    
    #strength = 'eco' if n_parts in [128, 256] else 'strong'
    strength = 'strong'
    sift_top_parts_path = osp.join(data_dir, 'partition_{}_{}'.format(n_parts, strength), 'partition.txt')
    
    return sift_top_parts_path

'''
Memory-compatible. 
Ranks of closest points not self.
Uses l2 dist. But uses cosine dist if data normalized. 
Input: 
-data: tensors
-specify k if only interested in the top k results.
-largest: whether pick largest when ranking. 
-include_self: include the point itself in the final ranking.
'''
def dist_rank(data_x, k, data_y=None, largest=False, opt=None, include_self=False):

    if isinstance(data_x, np.ndarray):
        data_x = torch.from_numpy(data_x)

    if data_y is None:
        data_y = data_x
    else:
        if isinstance(data_y, np.ndarray):
            data_y = torch.from_numpy(data_y)
    k0 = k
    device_o = data_x.device
    data_x = data_x.to(device)
    data_y = data_y.to(device)
    
    (data_x_len, dim) = data_x.size()
    data_y_len = data_y.size(0)
    #break into chunks. 5e6  is total for MNIST point size
    #chunk_sz = int(5e6 // data_y_len)
    chunk_sz = 16384
    chunk_sz = 500 #700 mem error. 1 mil points
    if data_y_len > 990000:
        chunk_sz = 600 #1000 if over 1.1 mil
        #chunk_sz = 500 #1000 if over 1.1 mil 
    else:
        chunk_sz = 3000    

    if k+1 > len(data_y):
        k = len(data_y) - 1
    #if opt is not None and opt.sift:
    
    if device == 'cuda':
        dist_mx = torch.cuda.LongTensor(data_x_len, k+1)
        act_dist = torch.cuda.FloatTensor(data_x_len, k+1)
    else:
        dist_mx = torch.LongTensor(data_x_len, k+1)
        act_dist = torch.cuda.FloatTensor(data_x_len, k+1)
    data_normalized = True if opt is not None and opt.normalize_data else False
    largest = True if largest else (True if data_normalized else False)
    
    #compute l2 dist <--be memory efficient by blocking
    total_chunks = int((data_x_len-1) // chunk_sz) + 1
    y_t = data_y.t()
    if not data_normalized:
        y_norm = (data_y**2).sum(-1).view(1, -1)
    
    for i in range(total_chunks):
        base = i*chunk_sz
        upto = min((i+1)*chunk_sz, data_x_len)
        cur_len = upto-base
        x = data_x[base : upto]
        
        if not data_normalized:
            x_norm = (x**2).sum(-1).view(-1, 1)        
            #plus op broadcasts
            dist = x_norm + y_norm        
            dist -= 2*torch.mm(x, y_t)
        else:
            dist = -torch.mm(x, y_t)
            
        topk_d, topk = torch.topk(dist, k=k+1, dim=1, largest=largest)
                
        dist_mx[base:upto, :k+1] = topk #torch.topk(dist, k=k+1, dim=1, largest=largest)[1][:, 1:]
        act_dist[base:upto, :k+1] = topk_d #torch.topk(dist, k=k+1, dim=1, largest=largest)[1][:, 1:]
        
    topk = dist_mx
    if k > 3 and opt is not None and opt.sift:
        #topk = dist_mx
        #sift contains duplicate points, don't run this in general.
        identity_ranks = torch.LongTensor(range(len(topk))).to(topk.device)
        topk_0 = topk[:, 0]
        topk_1 = topk[:, 1]
        topk_2 = topk[:, 2]
        topk_3 = topk[:, 3]

        id_idx1 = topk_1 == identity_ranks
        id_idx2 = topk_2 == identity_ranks
        id_idx3 = topk_3 == identity_ranks

        if torch.sum(id_idx1).item() > 0:
            topk[id_idx1, 1] = topk_0[id_idx1]

        if torch.sum(id_idx2).item() > 0:
            topk[id_idx2, 2] = topk_0[id_idx2]

        if torch.sum(id_idx3).item() > 0:
            topk[id_idx3, 3] = topk_0[id_idx3]           

    
    if not include_self:
        topk = topk[:, 1:]
        act_dist = act_dist[:, 1:]
    elif topk.size(-1) > k0:
        topk = topk[:, :-1]
    topk = topk.to(device_o)
    return act_dist, topk

'''
Memory-compatible. 
Input: 
-data: tensors
-data_y: if None take dist from data_x to itself
'''
def l2_dist(data_x, data_y=None):

    if data_y is not None:
        return _l2_dist2(data_x, data_y)
    else:
        return _l2_dist1(data_x)
   
'''
Memory-compatible, when insufficient GPU mem. To be combined with _l2_dist2 later.
Input: 
-data: tensor
'''
def _l2_dist1(data):

    if isinstance(data, numpy.ndarray):
        data = torch.from_numpy(data)
    (data_len, dim) = data.size()
    #break into chunks. 5e6  is total for MNIST point size
    chunk_sz = int(5e6 // data_len)    
    dist_mx = torch.FloatTensor(data_len, data_len)
    
    #compute l2 dist <--be memory efficient by blocking
    total_chunks = int((data_len-1) // chunk_sz) + 1
    y_t = data.t()
    y_norm = (data**2).sum(-1).view(1, -1)
    
    for i in range(total_chunks):
        base = i*chunk_sz
        upto = min((i+1)*chunk_sz, data_len)
        cur_len = upto-base
        x = data[base : upto]
        x_norm = (x**2).sum(-1).view(-1, 1)
        #plus op broadcasts
        dist_mx[base:upto] = x_norm + y_norm - 2*torch.mm(x, y_t)
        

    return dist_mx

'''
Memory-compatible.
Input: 
-data: tensor
'''
def _l2_dist2(data_x, data_y):

    (data_x_len, dim) = data_x.size()
    data_y_len = data_y.size(0)
    #break into chunks. 5e6  is total for MNIST point size
    chunk_sz = int(5e6 // data_y_len)
    dist_mx = torch.FloatTensor(data_x_len, data_y_len)
    
    #compute l2 dist <--be memory efficient by blocking
    total_chunks = int((data_x_len-1) // chunk_sz) + 1
    y_t = data_y.t()
    y_norm = (data_y**2).sum(-1).view(1, -1)
    
    for i in range(total_chunks):
        base = i*chunk_sz
        upto = min((i+1)*chunk_sz, data_x_len)
        cur_len = upto-base
        x = data_x[base : upto]
        x_norm = (x**2).sum(-1).view(-1, 1)
        #plus op broadcasts
        dist_mx[base:upto] = x_norm + y_norm - 2*torch.mm(x, y_t)
        
        #data_x = data[base : upto].unsqueeze(cur_len, data_len, dime(1).expand(cur_len, data_len, dim)
        #                                    )
    return dist_mx

 
'''
convert numpy array or list to markdown table
Input:
-numpy array (or two-nested list)
-s

'''
def mx2md(mx, row_label, col_label):
    #height, width = mx.shape
    height, width = len(mx), len(mx[0])
    
    if height != len(row_label) or width != len(col_label):
        raise Exception('mx2md: height != len(row_label) or width != len(col_label)')

    l = ['-']
    l.extend([str(i) for i in col_label])
    rows = [l]
    rows.append(['---' for i in range(width+1)])
    
    for i, row in enumerate(mx):
        l = [str(row_label[i])]
        l.extend([str(j) for j in mx[i]])
        rows.append(l)
        
    md = '\n'.join(['|'.join(row) for row in rows])
    #md0 = ['\n'.join(row) for row in rows]
    return md

'''
convert multiple numpy arrays or lists of same shape to markdown table
Input:
-numpy array (or two-nested list)

'''
def mxs2md(mx_l, row_label, col_label):
        
    height, width = len(mx_l[0]), len(mx_l[0][0])

    for i, mx in enumerate(mx_l, 1):
        if (height, width) != (len(mx), len(mx[0])):
            raise Exception('shape mismatch: height != len(row_label) or width != len(col_label)')
    
    if height != len(row_label) or width != len(col_label):
        raise Exception('mx2md: height != len(row_label) or width != len(col_label)')

    l = ['-']
    l.extend([str(i) for i in col_label])
    rows = [l]
    rows.append(['---' for i in range(width+1)])

    for i, row in enumerate(mx):
        l = [str(row_label[i])]
                    
        #l.extend([str(j) for j in mx_k[i]])
        l.extend([' / '.join([str(mx_k[i][j]) for mx_k in mx_l]) for j in range(width)])
        rows.append(l)
        
    md = '\n'.join(['|'.join(row) for row in rows])
    #md0 = ['\n'.join(row) for row in rows]
    return md

def load_lines(path):
    with open(path, 'r') as file:
        lines = file.read().splitlines()
    return lines

'''                            
Input: lines is list of objects, not newline-terminated yet.                                                                        
'''
def write_lines(lines, path):
    lines1 = []
    for line in lines:
        lines1.append(str(line) + os.linesep)
    with open(path, 'w') as file:
        file.writelines(lines1)

def pickle_dump(obj, path):
    with open(path, 'wb') as file:
        pickle.dump(obj, file)

def pickle_load(path):
    with open(path, 'rb') as file:
        return pickle.load(file)

    
if __name__ == '__main__':
    mx1 = np.zeros((2,2))
    mx2 = np.ones((2,2))
    
    row = ['1','2']
    col = ['3','4']
    
    print(mxs2md([mx1,mx2], row, col))

