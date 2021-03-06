import torch
import torch.nn as nn
import torch.nn.functional as F

import time
import logging

from utils import AvgrageMeter

class MixedOp(nn.Module):
  """Mixed operation.
  Weighted sum of blocks.
  """
  def __init__(self, blocks):
    super(MixedOp, self).__init__()
    self._ops = nn.ModuleList()
    for op in blocks:
      self._ops.append(op)

  def forward(self, x, weights):
    return sum(w * op(x) for w, op in zip(weights, self._ops))

class FBNet(nn.Module):

  def __init__(self, num_classes, blocks,
               init_theta=1.0,
               speed_f='./speed.txt',
               alpha=0.2,
               beta=0.6):
    super(FBNet, self).__init__()

    # if isinstance(init_theta, int):
    init_func = lambda x: nn.init.constant_(x, init_theta)
    
    self._alpha = alpha
    self._beta = beta
    self._criterion = nn.CrossEntropyLoss().cuda()

    self.theta = []
    self._ops = []
    self._blocks = blocks
    for b in blocks:
      if isinstance(b, list):
        num_block = len(b)
        theta = torch.ones((num_block, ), requires_grad=True)
        init_func(theta)
        self.theta.append(theta)

        self._ops.append(MixedOp(b))
    
    assert len(self.theta) == 22
    with open(speed_f, 'r') as f:
      self._speed = f.readlines()

    self.classifier = nn.Linear(1984, num_classes)

  def forward(self, input, target, temperature=5.0):
    batch_size = input.size()[0]
    data = self._blocks[0](input)
    theta_idx = 0
    lat = []
    for l_idx in range(1, len(self._blocks)):
      block = self._blocks[l_idx]
      if len(block) > 1:
        theta = self.theta[theta_idx]
        theta_idx += 1
        # t = theta.reshape(1, -1)
        t = theta.repeat(batch_size, 1)
        weight = nn.functional.gumbel_softmax(t,
                                temperature)
        speed = self._speed[theta_idx].strip().split(' ')
        speed = [float(tmp) for tmp in speed]
        lat_ = weight * torch.tensor(speed).repeat(batch_size, 1).sum()
        lat.append(lat_)

        data = self._ops[theta_idx](data, weight)
      else:
        data = block(data)

    lat = torch.tensor(lat)
    data = nn.avg_pool2d(data, data.size()[2:])
    logits = self.classifier(data)

    self.ce = self._criterion(logits, target).sum()
    self.lat_loss = torch.sum(lat)
    self.loss = self.ce +  self._alpha * self.lat_loss.pow(self._beta)

    pred = torch.argmax(logits, dim=1)
    self.acc = torch.sum(pred == target) / batch_size
    self.batch_size = batch_size
    return self.loss

class Trainer(object):
  """Training network parameters and theta separately.
  """
  def __init__(self, network,
               w_lr=0.01,
               w_mom=0.9,
               w_wd=1e-4,
               t_lr=0.001,
               t_wd=3e-3,
               init_temperature=5.0,
               temperature_decay=0.965,
               logger=logging):
    assert isinstance(network, FBNet)
    network.train()
    self._mod = network
    theta_params = network.theta
    mod_params = []
    for v in network.parameters():
      if v not in theta_params:
        mod_params.append(v)
    self.theta = theta_params
    self.w = mod_params
    self._tem_decay = temperature_decay
    self.temp = init_temperature
    self.logger = logger

    self._acc_avg = AvgrageMeter('acc')
    self._ce_avg = AvgrageMeter('ce')
    self._lat_avg = AvgrageMeter('lat')

    self.w_opt = torch.optim.SGD(
                    mod_params,
                    w_lr,
                    momentum=w_mom,
                    weight_decay=w_wd)
    
    self.t_opt = torch.optim.Adam(
                    theta_params,
                    lr=t_lr, betas=(0.5, 0.999), 
                    weight_decay=t_wd)
    
  def train_w(self, input, target, decay_temperature=True):
    """Update model parameters.
    """
    self.w_opt.zero_grad()
    loss = self._mod(input, target, self.temp)
    loss.backward()
    self.w_opt.step()
    if decay_temperature:
      self.temp *= self._tem_decay
  
  def train_t(self, input, target, decay_temperature=True):
    """Update theta.
    """
    self.t_opt.zero_grad()
    loss = self._mod(input, target, self.temp)
    loss.backward()
    self.t_opt.step()
    if decay_temperature:
      self.temp *= self._tem_decay
  
  def _step(self, input, target, 
            epoch, step,
            log_frequence,
            func):
    """Perform one step of training.
    """
    input = input.cuda()
    target = target.cuda()
    func(input, target)

    # Get status
    batch_size = self._mod.batch_size
    acc = self._mod.acc
    ce = self._mod.ce / batch_size
    lat = self._mod.lat_loss / batch_size

    self._acc_avg.update(acc, batch_size)
    self._ce_avg.update(ce, batch_size)
    self._lat_avg.update(lat, batch_size)

    if step > 1 and (step % log_frequence == 0):
      self.toc = time.time()
      speed = 1.0 * self._acc_avg.cnt / (self.toc - self.tic)

      self.logger.info("Epoch[{}] Batch[{}] Speed: {}samples/sec \
                      {} {} {}".format(epoch, step, speed,
        self._acc_avg, self._ce_avg, self._lat_avg))
      self.tic = time.time()
  
  def search(self, train_w_ds,
            train_t_ds,
            total_epoch=90,
            start_w_epoch=10,
            log_frequence=100):
    """Search model.
    """
    assert start_w_epoch >= 1, "Start to train w"
    self.tic = time.time()
    for epoch in range(start_w_epoch):
      for step, (input, target) in enumerate(train_w_ds):
        self._step(input, target, epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_w(x, y, False))
    
    self.tic = time.time()
    for epoch in range(total_epoch):
      for step, (input, target) in enumerate(train_t_ds):
        self._step(input, target, epoch + start_w_epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_t(x, y, True))

      for step, (input, target) in enumerate(train_w_ds):
        self._step(input, target, epoch + start_w_epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_w(x, y, False))

  def save_theta(self, save_path='theta.txt', epoch=-1):
    """Save theta.
    """
    res = []
    with open(save_path, 'w') as f:
      for t in self.theta:
        t_list = list(t)
        res.append(t_list)
        s = ' '.join([str(tmp) for tmp in t_list])
        f.write(s + '/n')
    return res
