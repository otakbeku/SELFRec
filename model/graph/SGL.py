from base.graph_recommender import GraphRecommender
import tensorflow as tf
from base.tf_interface import TFGraphInterface
from util.loss import bpr_loss,infoNCE
from util.conf import OptionConf
import numpy as np
import scipy.sparse as sp
import random
import os
from util.sampler import next_batch_pairwise
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


class SGL(GraphRecommender):
    def __init__(self, conf, training_set, test_set):
        super(SGL, self).__init__(conf, training_set, test_set)
        args = OptionConf(self.config['SGL'])
        self.ssl_reg = float(args['-lambda'])
        self.drop_rate = float(args['-droprate'])
        self.aug_type = int(args['-augtype'])
        self.ssl_temp = float(args['-temp'])
        self.n_layers = int(args['-n_layer'])

    def init(self):
        super(SGL, self).init()
        initializer = tf.contrib.layers.xavier_initializer()
        self.user_embeddings = tf.Variable(initializer([self.data.user_num, self.emb_size]))
        self.item_embeddings = tf.Variable(initializer([self.data.item_num, self.emb_size]))
        self.u_idx = tf.placeholder(tf.int32, name="u_idx")
        self.v_idx = tf.placeholder(tf.int32, name="v_idx")
        self.neg_idx = tf.placeholder(tf.int32, name="neg_holder")
        self.norm_adj = TFGraphInterface.create_joint_sparse_adj_tensor(self.data.norm_adj)
        ego_embeddings = tf.concat([self.user_embeddings,self.item_embeddings], axis=0)
        s1_embeddings = ego_embeddings
        s2_embeddings = ego_embeddings
        all_s1_embeddings = [s1_embeddings]
        all_s2_embeddings = [s2_embeddings]
        all_embeddings = [ego_embeddings]
        #variable initialization
        self._create_variable()
        for k in range(0, self.n_layers):
            if self.aug_type in [0, 1]:
                self.sub_mat['sub_mat_1%d' % k] = tf.SparseTensor(
                    self.sub_mat['adj_indices_sub1'],
                    self.sub_mat['adj_values_sub1'],
                    self.sub_mat['adj_shape_sub1'])
                self.sub_mat['sub_mat_2%d' % k] = tf.SparseTensor(
                    self.sub_mat['adj_indices_sub2'],
                    self.sub_mat['adj_values_sub2'],
                    self.sub_mat['adj_shape_sub2'])
            else:
                self.sub_mat['sub_mat_1%d' % k] = tf.SparseTensor(
                    self.sub_mat['adj_indices_sub1%d' % k],
                    self.sub_mat['adj_values_sub1%d' % k],
                    self.sub_mat['adj_shape_sub1%d' % k])
                self.sub_mat['sub_mat_2%d' % k] = tf.SparseTensor(
                    self.sub_mat['adj_indices_sub2%d' % k],
                    self.sub_mat['adj_values_sub2%d' % k],
                    self.sub_mat['adj_shape_sub2%d' % k])

        #s1 - view
        for k in range(self.n_layers):
            s1_embeddings = tf.sparse_tensor_dense_matmul(self.sub_mat['sub_mat_1%d' % k],s1_embeddings)
            all_s1_embeddings += [s1_embeddings]
        all_s1_embeddings = tf.stack(all_s1_embeddings, 1)
        all_s1_embeddings = tf.reduce_mean(all_s1_embeddings, axis=1, keepdims=False)
        self.s1_user_embeddings, self.s1_item_embeddings = tf.split(all_s1_embeddings, [self.data.user_num, self.data.item_num], 0)

        #s2 - view
        for k in range(self.n_layers):
            s2_embeddings = tf.sparse_tensor_dense_matmul(self.sub_mat['sub_mat_2%d' % k],s2_embeddings)
            all_s2_embeddings += [s2_embeddings]
        all_s2_embeddings = tf.stack(all_s2_embeddings, 1)
        all_s2_embeddings = tf.reduce_mean(all_s2_embeddings, axis=1, keepdims=False)
        self.s2_user_embeddings, self.s2_item_embeddings = tf.split(all_s2_embeddings, [self.data.user_num, self.data.item_num], 0)
        #recommendation view
        for k in range(self.n_layers):
            ego_embeddings = tf.sparse_tensor_dense_matmul(self.norm_adj,ego_embeddings)
            all_embeddings += [ego_embeddings]
        all_embeddings = tf.stack(all_embeddings, 1)
        all_embeddings = tf.reduce_mean(all_embeddings, axis=1, keepdims=False)
        self.main_user_embeddings, self.main_item_embeddings = tf.split(all_embeddings, [self.data.user_num, self.data.item_num], 0)
        self.neg_idx = tf.placeholder(tf.int32, name="neg_holder")
        self.batch_neg_item_emb = tf.nn.embedding_lookup(self.main_item_embeddings, self.neg_idx)
        self.batch_user_emb = tf.nn.embedding_lookup(self.main_user_embeddings, self.u_idx)
        self.batch_pos_item_emb = tf.nn.embedding_lookup(self.main_item_embeddings, self.v_idx)
        tf_config = tf.ConfigProto()
        tf_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=tf_config)

    def _convert_sp_mat_to_sp_tensor(self, X):
        coo = X.tocoo().astype(np.float32)
        indices = np.mat([coo.row, coo.col]).transpose()
        return tf.SparseTensor(indices, coo.data, coo.shape)

    def _convert_csr_to_sparse_tensor_inputs(self, X):
        coo = X.tocoo()
        indices = np.mat([coo.row, coo.col]).transpose()
        return indices, coo.data, coo.shape

    def _create_variable(self):
        self.sub_mat = {}
        if self.aug_type in [0, 1]:
            self.sub_mat['adj_values_sub1'] = tf.placeholder(tf.float32)
            self.sub_mat['adj_indices_sub1'] = tf.placeholder(tf.int64)
            self.sub_mat['adj_shape_sub1'] = tf.placeholder(tf.int64)

            self.sub_mat['adj_values_sub2'] = tf.placeholder(tf.float32)
            self.sub_mat['adj_indices_sub2'] = tf.placeholder(tf.int64)
            self.sub_mat['adj_shape_sub2'] = tf.placeholder(tf.int64)
        else:
            for k in range(self.n_layers):
                self.sub_mat['adj_values_sub1%d' % k] = tf.placeholder(tf.float32, name='adj_values_sub1%d' % k)
                self.sub_mat['adj_indices_sub1%d' % k] = tf.placeholder(tf.int64, name='adj_indices_sub1%d' % k)
                self.sub_mat['adj_shape_sub1%d' % k] = tf.placeholder(tf.int64, name='adj_shape_sub1%d' % k)

                self.sub_mat['adj_values_sub2%d' % k] = tf.placeholder(tf.float32, name='adj_values_sub2%d' % k)
                self.sub_mat['adj_indices_sub2%d' % k] = tf.placeholder(tf.int64, name='adj_indices_sub2%d' % k)
                self.sub_mat['adj_shape_sub2%d' % k] = tf.placeholder(tf.int64, name='adj_shape_sub2%d' % k)

    def _create_adj_mat(self, is_subgraph=False, aug_type=0):
        n_nodes = self.data.user_num + self.data.item_num
        row_idx = [self.data.user[pair[0]] for pair in self.data.training_data]
        col_idx = [self.data.item[pair[1]] for pair in self.data.training_data]
        if is_subgraph and aug_type in [0, 1, 2] and self.drop_rate > 0:
            # data augmentation type --- 0: Node Dropout; 1: Edge Dropout; 2: Random Walk
            if aug_type == 0:
                drop_user_idx = random.sample(list(range(self.data.user_num)), int(self.data.user_num * self.drop_rate))
                drop_item_idx = random.sample(list(range(self.data.item_num)), int(self.data.item_num * self.drop_rate))
                indicator_user = np.ones(self.data.user_num, dtype=np.float32)
                indicator_item = np.ones(self.data.item_num, dtype=np.float32)
                indicator_user[drop_user_idx] = 0.
                indicator_item[drop_item_idx] = 0.
                diag_indicator_user = sp.diags(indicator_user)
                diag_indicator_item = sp.diags(indicator_item)
                R = sp.csr_matrix(
                    (np.ones_like(row_idx, dtype=np.float32), (row_idx, col_idx)),
                    shape=(self.data.user_num, self.data.item_num))
                R_prime = diag_indicator_user.dot(R).dot(diag_indicator_item)
                (user_np_keep, item_np_keep) = R_prime.nonzero()
                ratings_keep = R_prime.data
                tmp_adj = sp.csr_matrix((ratings_keep, (user_np_keep, item_np_keep+self.data.user_num)), shape=(n_nodes, n_nodes))
            if aug_type in [1, 2]:
                keep_idx = random.sample(list(range(self.data.training_size()[-1])), int(self.data.training_size()[-1] * (1 - self.drop_rate)))
                user_np = np.array(row_idx)[keep_idx]
                item_np = np.array(col_idx)[keep_idx]
                ratings = np.ones_like(user_np, dtype=np.float32)
                tmp_adj = sp.csr_matrix((ratings, (user_np, item_np+self.data.user_num)), shape=(n_nodes, n_nodes))
        else:
            user_np = np.array(row_idx)
            item_np = np.array(col_idx)
            ratings = np.ones_like(user_np, dtype=np.float32)
            tmp_adj = sp.csr_matrix((ratings, (user_np, item_np+self.data.user_num)), shape=(n_nodes, n_nodes))
        adj_mat = tmp_adj + tmp_adj.T
        # pre adjcency matrix
        rowsum = np.array(adj_mat.sum(1))
        d_inv = np.power(rowsum, -0.5).flatten()
        d_inv[np.isinf(d_inv)] = 0.
        d_mat_inv = sp.diags(d_inv)
        norm_adj_tmp = d_mat_inv.dot(adj_mat)
        adj_matrix = norm_adj_tmp.dot(d_mat_inv)
        return adj_matrix

    def calc_ssl_loss(self):
        user_emb1 = tf.nn.embedding_lookup(self.s1_user_embeddings, tf.unique(self.u_idx)[0])
        user_emb2 = tf.nn.embedding_lookup(self.s2_user_embeddings, tf.unique(self.u_idx)[0])
        item_emb1 = tf.nn.embedding_lookup(self.s1_item_embeddings, tf.unique(self.v_idx)[0])
        item_emb2 = tf.nn.embedding_lookup(self.s2_item_embeddings, tf.unique(self.v_idx)[0])
        emb_merge1 = tf.concat([user_emb1, item_emb1], axis=0)
        emb_merge2 = tf.concat([user_emb2, item_emb2], axis=0)
        ssl_loss = self.ssl_reg * infoNCE(emb_merge1,emb_merge2,0.2)
        return ssl_loss

    def train(self):
        #main task: recommendation
        rec_loss = bpr_loss(self.batch_user_emb,self.batch_pos_item_emb,self.batch_neg_item_emb)
        rec_loss +=  self.reg * (tf.nn.l2_loss(self.batch_user_emb) + tf.nn.l2_loss(self.batch_pos_item_emb) + tf.nn.l2_loss(self.batch_neg_item_emb))
        #SSL task: contrastive learning
        ssl_loss = self.calc_ssl_loss()
        total_loss = rec_loss+ssl_loss

        opt = tf.train.AdamOptimizer(self.lRate)
        train = opt.minimize(total_loss)

        init = tf.global_variables_initializer()
        self.sess.run(init)
        import time
        for epoch in range(self.maxEpoch):
            sub_mat = {}
            if self.aug_type in [0, 1]:
                sub_mat['adj_indices_sub1'], sub_mat['adj_values_sub1'], sub_mat[
                    'adj_shape_sub1'] = self._convert_csr_to_sparse_tensor_inputs(
                    self._create_adj_mat(is_subgraph=True, aug_type=self.aug_type))

                sub_mat['adj_indices_sub2'], sub_mat['adj_values_sub2'], sub_mat[
                    'adj_shape_sub2'] = self._convert_csr_to_sparse_tensor_inputs(
                    self._create_adj_mat(is_subgraph=True, aug_type=self.aug_type))
            else:
                for k in range(self.n_layers):
                    sub_mat['adj_indices_sub1%d' % k], sub_mat['adj_values_sub1%d' % k], sub_mat[
                        'adj_shape_sub1%d' % k] = self._convert_csr_to_sparse_tensor_inputs(
                        self._create_adj_mat(is_subgraph=True, aug_type=self.aug_type))
                    sub_mat['adj_indices_sub2%d' % k], sub_mat['adj_values_sub2%d' % k], sub_mat[
                        'adj_shape_sub2%d' % k] = self._convert_csr_to_sparse_tensor_inputs(
                        self._create_adj_mat(is_subgraph=True, aug_type=self.aug_type))

            for n, batch in enumerate(next_batch_pairwise(self.data,self.batch_size)):
                user_idx, i_idx, j_idx = batch
                feed_dict = {self.u_idx: user_idx,
                             self.v_idx: i_idx,
                             self.neg_idx: j_idx, }
                if self.aug_type in [0, 1]:
                    feed_dict.update({
                        self.sub_mat['adj_values_sub1']: sub_mat['adj_values_sub1'],
                        self.sub_mat['adj_indices_sub1']: sub_mat['adj_indices_sub1'],
                        self.sub_mat['adj_shape_sub1']: sub_mat['adj_shape_sub1'],
                        self.sub_mat['adj_values_sub2']: sub_mat['adj_values_sub2'],
                        self.sub_mat['adj_indices_sub2']: sub_mat['adj_indices_sub2'],
                        self.sub_mat['adj_shape_sub2']: sub_mat['adj_shape_sub2']
                    })
                else:
                    for k in range(self.n_layers):
                        feed_dict.update({
                            self.sub_mat['adj_values_sub1%d' % k]: sub_mat['adj_values_sub1%d' % k],
                            self.sub_mat['adj_indices_sub1%d' % k]: sub_mat['adj_indices_sub1%d' % k],
                            self.sub_mat['adj_shape_sub1%d' % k]: sub_mat['adj_shape_sub1%d' % k],
                            self.sub_mat['adj_values_sub2%d' % k]: sub_mat['adj_values_sub2%d' % k],
                            self.sub_mat['adj_indices_sub2%d' % k]: sub_mat['adj_indices_sub2%d' % k],
                            self.sub_mat['adj_shape_sub2%d' % k]: sub_mat['adj_shape_sub2%d' % k]
                        })

                _, l,rec_l,ssl_l = self.sess.run([train, total_loss, rec_loss, ssl_loss],feed_dict=feed_dict)
                print('training:', epoch + 1, 'batch', n, 'rec_loss:', rec_l, 'ssl_loss',ssl_l)
            self.U, self.V = self.sess.run([self.main_user_embeddings, self.main_item_embeddings])
            self.training_evaluation(epoch)
        self.U, self.V = self.best_user_emb, self.best_item_emb

    def save(self):
        self.best_user_emb, self.best_item_emb = self.sess.run([self.main_user_embeddings, self.main_item_embeddings])

    def predict(self, u):
        'rank all the items for the user'
        if self.data.contain_user(u):
            u = self.data.get_user_id(u)
            return self.V.dot(self.U[u])
        else:
            return [0] * self.data.item_num