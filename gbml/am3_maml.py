# Code is based on https://github.com/sungyubkim/GBML
# Here is our the main contribution that the initialization is made from word embeddings of labels

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import higher

from gbml.gbml import GBML
from utils import get_accuracy, apply_grad, mix_grad, grad_to_cos, loss_to_ent

class AM3_MAML(GBML):

    def __init__(self, args):
        super().__init__(args)
        self._init_net()
        return None

    @torch.enable_grad()
    def inner_loop(self, inner_param, diffopt, train_input, train_target, word_proto):

        train_logit = torch.matmul(train_input, 2 * word_proto * inner_param[0]) \
                        - ((word_proto * inner_param[0])**2).sum(dim=0, keepdim=True) + inner_param[1]
        inner_loss = F.cross_entropy(train_logit, train_target)
        diffopt.step(inner_loss)

        return None

    def outer_loop(self, batch, reverse_dict_list, is_train):

        self.network.zero_grad()
        
        train_inputs, train_targets, test_inputs, test_targets = self.unpack_batch(batch)

        loss_log = 0
        acc_log = 0
        grad_list = []
        loss_list = []
        for i, (train_input, train_target, test_input, test_target) in enumerate(zip(train_inputs, train_targets, test_inputs, test_targets)):
            self.network.init_decoder()
            inner_optimizer = torch.optim.SGD(self.network.decoder, lr=self.args.inner_lr)
            with higher.innerloop_ctx(self.network, inner_optimizer, track_higher_grads=is_train) as (fmodel, diffopt):

                fmodel(torch.zeros(1,3,80,80).type(torch.float32).cuda())

                # Convert numerical label to word label
                target = [[reverse_dict_list[i][j] for j in range(self.args.num_way)]]

                # Get transformed word embeddings and lambda
                word_proto = fmodel.word_embedding(target, is_train)[0].permute([1,0])

                train_input = fmodel(train_input)
                for step in range(self.args.n_inner):
                    self.inner_loop(fmodel.decoder, diffopt, train_input, train_target, word_proto)

                test_logit = fmodel(test_input)
                test_logit = torch.matmul(test_logit, 2 * word_proto * fmodel.decoder[0]) \
                            - ((word_proto * fmodel.decoder[0])**2).sum(dim=0, keepdim=True) + fmodel.decoder[1]
                outer_loss = F.cross_entropy(test_logit, test_target)
                loss_log += outer_loss.item()/self.batch_size

                with torch.no_grad():
                    acc_log += get_accuracy(test_logit, test_target).item()/self.batch_size
            
                if is_train:
                    params = fmodel.parameters(time=0)
                    outer_grad = torch.autograd.grad(outer_loss, params)
                    grad_list.append(outer_grad)
                    loss_list.append(outer_loss.item())
        
        # self._lambda = _lambda.detach()

        if is_train:
            weight = torch.ones(len(grad_list))
            weight = weight / torch.sum(weight)
            grad = mix_grad(grad_list, weight)
            grad_log = apply_grad(self.network, grad)
            self.outer_optimizer.step()
            
            return loss_log, acc_log, grad_log
        else:
            return loss_log, acc_log

    def _init_opt(self):
        if self.args.outer_opt == 'SGD':
            self.outer_optimizer = torch.optim.SGD(self.network.parameters(), lr=self.args.outer_lr, nesterov=True, momentum=0.9)
        elif self.args.outer_opt == 'Adam':
            self.outer_optimizer = torch.optim.Adam(self.network.parameters(), lr=self.args.outer_lr)
        else:
            raise ValueError('Not supported outer optimizer.')
        self.lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(self.outer_optimizer, \
            milestones=[int(self.args.num_epoch*6/8)], gamma=0.1)
        # self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.outer_optimizer, step_size=10, gamma=0.5)
        return None
