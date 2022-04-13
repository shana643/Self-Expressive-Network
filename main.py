import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch.optim as optim
import utils
from sklearn import cluster
import pickle
import scipy.sparse as sparse
import time
from sklearn.preprocessing import normalize
from sklearn.neighbors import kneighbors_graph
from sklearn.metrics import normalized_mutual_info_score, adjusted_rand_score
from metrics.cluster.accuracy import clustering_accuracy
import argparse
import random
from tqdm import tqdm
import os
import csv
os.environ["CUDA_VISIBLE_DEVICES"] = "0"


class Self_Attn(nn.Module):
    """ Self attention Layer"""

    def __init__(self, in_dim):
        super(Self_Attn, self).__init__()
        self.chanel_in = in_dim
        #self.activation = activation

        self.query_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim // 8, kernel_size=1)
        self.key_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim // 8, kernel_size=1)
        self.value_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim, kernel_size=1)
        self.gamma = nn.Parameter(torch.zeros(1))

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x):
        """
            inputs :
                x : input feature maps( B X C X W X H)
            returns :
                out : self attention value + input feature
                attention: B X N X N (N is Width*Height)
        """
        m_batchsize, C, width, height = x.size()
        proj_query = self.query_conv(x).view(m_batchsize, -1, width * height).permute(0, 2, 1)  # B X CX(N)
        proj_key = self.key_conv(x).view(m_batchsize, -1, width * height)  # B X C x (*W*H)
        energy = torch.bmm(proj_query, proj_key)  # transpose check
        attention = self.softmax(energy)  # BX (N) X (N)
        proj_value = self.value_conv(x).view(m_batchsize, -1, width * height)  # B X C X N

        out = torch.bmm(proj_value, attention.permute(0, 2, 1))
        out = out.view(m_batchsize, C, width, height)

        #out = self.gamma * out + x
        return out
class MLP(nn.Module):
    
    def __init__(self, input_dims, hid_dims, out_dims, kaiming_init=False):
        super(MLP, self).__init__()   #初始化 建立模型
        self.input_dims = input_dims #网络组件 输入层 隐藏层 输出层
        self.hid_dims = hid_dims
        self.output_dims = out_dims
        self.layers = nn.ModuleList()  #modulelist辅助式，一次性建立多个相同结构 像list
        
        self.layers.append(nn.Linear(self.input_dims, self.hid_dims[0]))  #线性回归
        self.layers.append(nn.ReLU())  #线性整流激活函数RELU ，输入向量经过该函数回输出至下一层
        for i in range(len(hid_dims) - 1):
            self.layers.append(nn.Linear(self.hid_dims[i], self.hid_dims[i + 1]))
            self.layers.append(nn.ReLU())

        self.out_layer = nn.Linear(self.hid_dims[-1], self.output_dims)
        if kaiming_init:
            self.reset_parameters()  #参数初始化
        
    def reset_parameters(self):
        for layer in self.layers:
            if isinstance(layer, nn.Linear):
                init.kaiming_uniform_(layer.weight)
                init.zeros_(layer.bias)
        init.xavier_uniform_(self.out_layer.weight)
        init.zeros_(self.out_layer.bias)
        
    def forward(self, x): #前向传播方法 ？
        h = x
        for i, layer in enumerate(self.layers):
            h = layer(h)
        h = self.out_layer(h)
        h = torch.tanh_(h) #双曲正弦 激活函数
        return h


class AdaptiveSoftThreshold(nn.Module):  #软阈值算子
    def __init__(self, dim):
        super(AdaptiveSoftThreshold, self).__init__()
        self.dim = dim
        self.register_parameter("bias", nn.Parameter(torch.from_numpy(np.zeros(shape=[self.dim])).float()))
    
    def forward(self, c):
        return torch.sign(c) * torch.relu(torch.abs(c) - self.bias)


class SENet(nn.Module):

    def __init__(self, input_dims, hid_dims, out_dims, kaiming_init=True):
        super(SENet, self).__init__()
        self.input_dims = input_dims
        self.hid_dims = hid_dims
        self.out_dims = out_dims
        self.kaiming_init = kaiming_init #
        self.shrink = 1.0 / out_dims 

        self.net_q = MLP(input_dims=self.input_dims,
                         hid_dims=self.hid_dims,
                         out_dims=self.out_dims,
                         kaiming_init=self.kaiming_init)

        self.net_k = MLP(input_dims=self.input_dims,
                         hid_dims=self.hid_dims,
                         out_dims=self.out_dims,
                         kaiming_init=self.kaiming_init)

        self.thres = AdaptiveSoftThreshold(1)

    def query_embedding(self, queries):
        q_emb = self.net_q(queries)
        return q_emb
    
    def key_embedding(self, keys):
        k_emb = self.net_k(keys)
        return k_emb

    def get_coeff(self, q_emb, k_emb):
        c = self.thres(q_emb.mm(k_emb.t())) #qk（T）软阈值
        return self.shrink * c #shrink=1/out.dims

    def forward(self, queries, keys):
        q = self.query_embedding(queries)
        k = self.key_embedding(keys)
        out = self.get_coeff(q_emb=q, k_emb=k)
        return out


def regularizer(c, lmbd=1.0):
    return lmbd * torch.abs(c).sum() + (1.0 - lmbd) / 2.0 * torch.pow(c, 2).sum()


def get_sparse_rep(senet, data, batch_size=10, chunk_size=100, non_zeros=1000): #获得稀疏表示  
    N, D = data.shape
    non_zeros = min(N, non_zeros) #选择最小的1000个非零系数
    C = torch.empty([batch_size, N])  #把C中这些系数置0
    if (N % batch_size != 0):
        raise Exception("batch_size should be a factor of dataset size.")
    if (N % chunk_size != 0):
        raise Exception("chunk_size should be a factor of dataset size.")

    val = []
    indicies = []
    with torch.no_grad():
        senet.eval() #执行一个字符串表达式
        for i in range(data.shape[0] // batch_size):
            chunk = data[i * batch_size:(i + 1) * batch_size].cuda() #将内存中的数据分批(batch_size)送到显存中进行运算
            q = senet.query_embedding(chunk)
            for j in range(data.shape[0] // chunk_size):
                chunk_samples = data[j * chunk_size: (j + 1) * chunk_size].cuda() #动态调度让每一条线程执行通过块大小（chunk-size）（默认为1）指定数量的迭代
                k = senet.key_embedding(chunk_samples)   
                temp = senet.get_coeff(q, k) #temp为自表达系数
                C[:, j * chunk_size:(j + 1) * chunk_size] = temp.cpu()#按列将得到的分块的coeff输入矩阵C 构建自表达矩阵C

            rows = list(range(batch_size))
            cols = [j + i * batch_size for j in rows]
            C[rows, cols] = 0.0 #对角线置0

            _, index = torch.topk(torch.abs(C), dim=1, k=non_zeros)
            
            val.append(C.gather(1, index).reshape([-1]).cpu().data.numpy())
            index = index.reshape([-1]).cpu().data.numpy()
            indicies.append(index)
    
    val = np.concatenate(val, axis=0) #拼接
    indicies = np.concatenate(indicies, axis=0)
    indptr = [non_zeros * i for i in range(N + 1)]
    
    C_sparse = sparse.csr_matrix((val, indicies, indptr), shape=[N, N]) #矩阵的压缩存储
    return C_sparse


def get_knn_Aff(C_sparse_normalized, k=3, mode='symmetric'):
    C_knn = kneighbors_graph(C_sparse_normalized, k, mode='connectivity', include_self=False, n_jobs=10)
    if mode == 'symmetric': #对称的
        Aff_knn = 0.5 * (C_knn + C_knn.T) #C+C的转置
    elif mode == 'reciprocal':
        Aff_knn = C_knn.multiply(C_knn.T)
    else:
        raise Exception("Mode must be 'symmetric' or 'reciprocal'")
    return Aff_knn


def evaluate(senet, data, labels, num_subspaces, spectral_dim, non_zeros=1000, n_neighbors=3,
             batch_size=10000, chunk_size=10000, affinity='nearest_neighbor', knn_mode='symmetric'):
    C_sparse = get_sparse_rep(senet=senet, data=data, batch_size=batch_size,
                              chunk_size=chunk_size, non_zeros=non_zeros)
    C_sparse_normalized = normalize(C_sparse).astype(np.float32)
    if affinity == 'symmetric':
        Aff = 0.5 * (np.abs(C_sparse_normalized) + np.abs(C_sparse_normalized).T)
    elif affinity == 'nearest_neighbor':
        Aff = get_knn_Aff(C_sparse_normalized, k=n_neighbors, mode=knn_mode)
    else:
        raise Exception("affinity should be 'symmetric' or 'nearest_neighbor'")
    preds = utils.spectral_clustering(Aff, num_subspaces, spectral_dim) #谱聚类 Aff是距离矩阵 num_subspaces为聚类维数
    acc = clustering_accuracy(labels, preds)  #三个评估指标
    nmi = normalized_mutual_info_score(labels, preds, average_method='geometric')
    ari = adjusted_rand_score(labels, preds)
    return acc, nmi, ari


def same_seeds(seed):  #随机种子，为了保证模型的可复现性
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


if __name__ == "__main__": 
    parser = argparse.ArgumentParser() #创建一个解析器
    parser.add_argument('--dataset', type=str, default="MNIST") #添加参数
    parser.add_argument('--num_subspaces', type=int, default=10) #聚类维数
    parser.add_argument('--gamma', type=float, default=200.0)
    parser.add_argument('--lmbd', type=float, default=0.9)
    parser.add_argument('--hid_dims', type=int, default=[1024, 1024, 1024])
    parser.add_argument('--out_dims', type=int, default=1024)
    parser.add_argument('--total_iters', type=int, default=100000)
    parser.add_argument('--save_iters', type=int, default=200000)
    parser.add_argument('--eval_iters', type=int, default=200000)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--lr_min', type=float, default=0.0)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--chunk_size', type=int, default=10000)
    parser.add_argument('--non_zeros', type=int, default=1000)
    parser.add_argument('--n_neighbors', type=int, default=3) #KNN算法K的个数
    parser.add_argument('--spectral_dim', type=int, default=15)#？？
    parser.add_argument('--affinity', type=str, default="nearest_neighbor") #KNN方法构建距离矩阵
    parser.add_argument('--mean_subtract', dest='mean_subtraction', action='store_true')
    parser.set_defaults(mean_subtraction=False)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args() #解析参数

    if args.dataset == 'MNIST' or args.dataset == 'FashionMNIST':
        args.__setattr__('gamma', 200.0)
        args.__setattr__('spectral_dim', 15)
        args.__setattr__('mean_subtract', False)
        args.__setattr__('lr_min', 0.0)
    elif args.dataset == 'EMNIST':
        args.__setattr__('gamma', 150.0)
        args.__setattr__('num_subspaces', 26)
        args.__setattr__('spectral_dim', 26)
        args.__setattr__('mean_subtract', True)
        args.__setattr__('chunk_size', 10611)
        args.__setattr__('lr_min', 1e-3)
    elif args.dataset == 'CIFAR10':
        args.__setattr__('gamma', 200)
        args.__setattr__('num_subspaces', 10)
        args.__setattr__('chunk_size', 10000)
        args.__setattr__('total_iters', 50000)
        args.__setattr__('eval_iters', 100000)
        args.__setattr__('lr_min', 0.0)
        args.__setattr__('spectral_dim', 10)
        args.__setattr__('mean_subtract', False)
        args.__setattr__('affinity', 'symmetric')
    else:
        raise Exception("Only MNIST, FashionMNIST, EMNIST and CIFAR10 are currently supported.")

    fit_msg = "Experiments on {}, numpy_seed=0, total_iters=100000, lambda=0.9, gamma=200.0".format(args.dataset, args.seed) #？？
    print(fit_msg)

    folder = "{}_result".format(args.dataset)
    if not os.path.exists(folder):
        os.mkdir(folder)

    same_seeds(args.seed)
    tic = time.time() #返回时间戳

    if args.dataset in ["MNIST", "FashionMNIST", "EMNIST"]:
        with open('datasets/{}/{}_scattering_train_data.pkl'.format(args.dataset, args.dataset), 'rb') as f:
            train_samples = pickle.load(f)  #从pickle格式的文件中读取数据并转换为Python的类型
        with open('datasets/{}/{}_scattering_train_label.pkl'.format(args.dataset, args.dataset), 'rb') as f:
            train_labels = pickle.load(f)
        with open('datasets/{}/{}_scattering_test_data.pkl'.format(args.dataset, args.dataset), 'rb') as f:
            test_samples = pickle.load(f)
        with open('datasets/{}/{}_scattering_test_label.pkl'.format(args.dataset, args.dataset), 'rb') as f:
            test_labels = pickle.load(f)
        full_samples = np.concatenate([train_samples, test_samples], axis=0)
        full_labels = np.concatenate([train_labels, test_labels], axis=0)
    elif args.dataset in ["CIFAR10"]:
        with open('datasets/CIFAR10-MCR2/cifar10-features.npy', 'rb') as f:
            full_samples = np.load(f)
        with open('datasets/CIFAR10-MCR2/cifar10-labels.npy', 'rb') as f:
            full_labels = np.load(f)
    else:
        raise Exception("Only MNIST, FashionMNIST and EMNIST are currently supported.")
    
    if args.mean_subtract: #均值滤波
        print("Mean Subtraction")
        full_samples = full_samples - np.mean(full_samples, axis=0, keepdims=True)  # mean subtraction
    
    full_labels = full_labels - np.min(full_labels) # 计算sre时需要label的范围是 0 ~ num_subspaces - 1

    result = open('{}/results.csv'.format(folder), 'w')#存输出
    writer = csv.writer(result)
    writer.writerow(["N", "ACC", "NMI", "ARI"])  #三个参数 来表示聚类性能

    global_steps = 0
    for N in [200, 500, 1000, 2000, 5000, 10000, 20000]:
        sampled_idx = np.random.choice(full_samples.shape[0], N, replace=False) #随机选N个样本
        samples, labels = full_samples[sampled_idx], full_labels[sampled_idx] 
        block_size = min(N, 10000)
      
        with open('{}/{}_samples_{}.pkl'.format(folder, args.dataset, N), 'wb') as f:
            pickle.dump(samples, f) #将Python数据转换并保存到pickle格式的文件内。
        with open('{}/{}_labels_{}.pkl'.format(folder, args.dataset, N), 'wb') as f:
            pickle.dump(labels, f)    

        all_samples, ambient_dim = samples.shape[0], samples.shape[1] #shape[0]表示图像高度（矩阵的行数） shape[1]表示宽度 第一维 第二维尺寸大小

        data = torch.from_numpy(samples).float()
        data = utils.p_normalize(data)

        n_iter_per_epoch = samples.shape[0] // args.batch_size #每次训练在训练集中取batchsize个样本训练
        n_step_per_iter = round(all_samples // block_size)  #1个iteration等于使用batchsize个样本训练一次
        n_epochs = args.total_iters // n_iter_per_epoch  #1个epoch等于使用训练集中的全部样本训练一次
        
        senet = SENet(ambient_dim, args.hid_dims, args.out_dims, kaiming_init=True).cuda() #经过网络
        optimizer = optim.Adam(senet.parameters(), lr=args.lr) #Adam优化器
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=n_epochs, eta_min=args.lr_min) #余弦退火

        n_iters = 0
        pbar = tqdm(range(n_epochs), ncols=120) #进度条

        for epoch in pbar:
            pbar.set_description(f"Epoch {epoch}")
            randidx = torch.randperm(data.shape[0])  #随机数字序列
            
            for i in range(n_iter_per_epoch): #训练模型
                senet.train()

                batch_idx = randidx[i * args.batch_size : (i + 1) * args.batch_size] 
                batch = data[batch_idx].cuda() #从cpu转到GPU 加速
                q_batch = senet.query_embedding(batch) 
                k_batch = senet.key_embedding(batch)
                
                rec_batch = torch.zeros_like(batch).cuda()
                reg = torch.zeros([1]).cuda()
                for j in range(n_step_per_iter):
                    block = data[j * block_size: (j + 1) * block_size].cuda()
                    k_block = senet.key_embedding(block) #用block算 降低计算量
                    c = senet.get_coeff(q_batch, k_block)  #乘积
                    rec_batch = rec_batch + c.mm(block)
                    reg = reg + regularizer(c, args.lmbd)  #正则化
                
                diag_c = senet.thres((q_batch * k_batch).sum(dim=1, keepdim=True)) * senet.shrink
                rec_batch = rec_batch - diag_c * batch
                reg = reg - regularizer(diag_c, args.lmbd)
                
                rec_loss = torch.sum(torch.pow(batch - rec_batch, 2))  #损失函数
                loss = (0.5 * args.gamma * rec_loss + reg) / args.batch_size

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(senet.parameters(), 0.001)
                optimizer.step()

                global_steps += 1
                n_iters += 1

                if n_iters % args.save_iters == 0:
                    with open('{}/SENet_{}_N{:d}_iter{:d}.pth.tar'.format(folder, args.dataset, N, n_iters), 'wb') as f:
                        torch.save(senet.state_dict(), f)
                    print("Model Saved.")

                if n_iters % args.eval_iters == 0:
                    print("Evaluating on sampled data...")
                    acc, nmi, ari = evaluate(senet, data=data, labels=labels, num_subspaces=args.num_subspaces, affinity=args.affinity,
                                            spectral_dim=args.spectral_dim, non_zeros=args.non_zeros, n_neighbors=args.n_neighbors,
                                            batch_size=block_size, chunk_size=block_size,
                                            knn_mode='symmetric')
                    print("ACC-{:.6f}, NMI-{:.6f}, ARI-{:.6f}".format(acc, nmi, ari))
                    
            pbar.set_postfix(loss="{:3.4f}".format(loss.item()),
                             rec_loss="{:3.4f}".format(rec_loss.item() / args.batch_size),
                             reg="{:3.4f}".format(reg.item() / args.batch_size))
            scheduler.step()

        print("Evaluating on {}-full...".format(args.dataset))
        full_data = torch.from_numpy(full_samples).float()
        full_data = utils.p_normalize(full_data)
        acc, nmi, ari = evaluate(senet, data=full_data, labels=full_labels, num_subspaces=args.num_subspaces, affinity=args.affinity,
                                spectral_dim=args.spectral_dim, non_zeros=args.non_zeros, n_neighbors=args.n_neighbors, batch_size=args.chunk_size,
                                chunk_size=args.chunk_size, knn_mode='symmetric')
        print("N-{:d}: ACC-{:.6f}, NMI-{:.6f}, ARI-{:.6f}".format(N, acc, nmi, ari))
        writer.writerow([N, acc, nmi, ari])
        result.flush()

        with open('{}/SENet_{}_N{:d}.pth.tar'.format(folder, args.dataset, N), 'wb') as f:
            torch.save(senet.state_dict(), f)

        torch.cuda.empty_cache()
    result.close()
