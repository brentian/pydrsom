"""
A quickstart script for DRSOM on Fashion-MNIST dataset.
@author: Chuwen Zhang
@note:
  This script runs DRSOM and compares to Adam, SGD, and so forth.
  ################################################################
    usage:
      $ python quickstart.py -h
  ################################################################
  You can use an easy/complex model by option --model {simple,cnn}
"""

import argparse
import json
import os
import time
from torch.utils.tensorboard import SummaryWriter
from pprint import pprint

import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision.transforms import ToTensor

from pydrsom.drsom import DRSOMF
from pydrsom.drsomnd import DRSOMF as DRSOMF3
from pydrsom.drsom_utils import *

parser = argparse.ArgumentParser(
  formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--optim",
                    required=False,
                    type=str,
                    default='drsom',
                    choices=[
                      'adam', 'sgd4',
                      'drsom', 'drsomnd', 'drsomq'
                    ])
parser.add_argument("--model",
                    required=False,
                    type=str,
                    default='simple',
                    choices=['cnn', 'simple'])
parser.add_argument("--data_size", required=False, type=int, default=int(1e3))
parser.add_argument("--batch", required=False, type=int, default=128)
parser.add_argument("--epoch", required=False, type=int, default=5)
parser.add_argument("--interval", required=False, type=int, default=20)
parser.add_argument("--bool_decay", required=False, type=int, default=1)
parser.add_argument("--option_tr",
                    required=False,
                    type=str,
                    default='p',
                    choices=['a', 'p'])
parser.add_argument("--hessian_window", required=False, type=int, default=1)
parser.add_argument('--drsom_beta1', default=50, type=float, help='DRSOM coefficients beta_1')
parser.add_argument('--drsom_beta2', default=30, type=float, help='DRSOM coefficients beta_2')
parser.add_argument("--itermax", required=False, type=int, default=15,
                    help='maximum iteration for inner optimization, for LBFGS and DRSOM')
parser.add_argument('--tflogger', default="run", type=str, help='tf logger')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--ckpt_name', type=str, help='resume from checkpoint')


class CNNModel(nn.Module):
  def __init__(self):
    super(CNNModel, self).__init__()
    
    self.cnn1 = nn.Conv2d(in_channels=1,
                          out_channels=32,
                          kernel_size=5,
                          stride=1,
                          padding=2)
    self.relu1 = nn.ReLU()
    self.norm1 = nn.BatchNorm2d(32)
    nn.init.xavier_uniform(self.cnn1.weight)
    
    self.maxpool1 = nn.MaxPool2d(kernel_size=2)
    
    self.cnn2 = nn.Conv2d(in_channels=32,
                          out_channels=64,
                          kernel_size=3,
                          stride=1,
                          padding=2)
    self.relu2 = nn.ReLU()
    self.norm2 = nn.BatchNorm2d(64)
    nn.init.xavier_uniform(self.cnn2.weight)
    
    self.maxpool2 = nn.MaxPool2d(kernel_size=2)
    
    self.fc1 = nn.Linear(4096, 4096)
    self.fcrelu = nn.ReLU()
    
    self.fc2 = nn.Linear(4096, 10)
  
  def forward(self, x):
    out = self.cnn1(x)
    out = self.relu1(out)
    out = self.norm1(out)
    
    out = self.maxpool1(out)
    
    out = self.cnn2(out)
    out = self.relu2(out)
    out = self.norm2(out)
    
    out = self.maxpool2(out)
    
    out = out.view(out.size(0), -1)
    
    out = self.fc1(out)
    out = self.fcrelu(out)
    
    out = self.fc2(out)
    return out


class VanillaNetwork(nn.Module):
  def __init__(self):
    super(VanillaNetwork, self).__init__()
    self.flatten = nn.Flatten()
    
    # case II: seems ok:
    self.layers = nn.Sequential(
      nn.Linear(28 * 28, 512),
      nn.Linear(512, 10),
    )
  
  def forward(self, x):
    x = self.flatten(x)
    logits = self.layers(x)
    return logits


def train(dataloader, name, model, loss_fn, optimizer, ninterval):
  st = time.time()
  size = len(dataloader.dataset)
  model.train()
  correct = 0
  total = 0
  avg_loss = 0
  for batch, (X, y) in enumerate(dataloader):
    X, y = X.to(device), y.to(device)
    
    def closure(backward=True):
      optimizer.zero_grad()
      output = model(X)
      loss = loss_fn(output, y)
      if not backward:
        return loss
      if name.startswith('drsom'):
        loss.backward(create_graph=True)
      else:
        loss.backward()
      return loss
    
    # backpropagation
   
    loss = optimizer.step(closure=closure)
    avg_loss += loss.item()
    
    # compute prediction error
    outputs = model(X)
    _, predicted = outputs.max(1)
    total += y.size(0)
    correct += predicted.eq(y).sum().item()
    
    if batch % ninterval == 0:
      loss, current = loss.item(), batch * len(X)
      print(f"loss: {loss:>7f}  [{current:>5d}/{size:>5d}]")
  
  accuracy = 100. * correct / total
  avg_loss = avg_loss / len(dataloader)
  print('train acc %.5f' % accuracy)
  print('train avg_loss %.5f' % avg_loss)
  print('train batches: ', len(dataloader))
  
  et = time.time()
  return et - st, avg_loss, accuracy


def test(dataloader, model, loss_fn):
  size = len(dataloader.dataset)
  num_batches = len(dataloader)
  model.eval()
  test_loss, correct = 0, 0
  with torch.no_grad():
    for batch, (X, y) in enumerate(dataloader):
      X, y = X.to(device), y.to(device)
      outputs = model(X)
      _, predicted = outputs.max(1)
      test_loss += loss_fn(outputs, y).item()
      correct += predicted.eq(y).sum().item()
  test_loss /= num_batches
  correct /= size
  rstring = f"Error: \n Accuracy: {(100 * correct):>0.1f}%, Avg loss: {test_loss:>8f} \n"
  print(rstring)
  result = {'acc': (100 * correct), 'avg_loss': test_loss}
  return result


if __name__ == '__main__':
  
  args = parser.parse_args()
  writer = SummaryWriter(log_dir=args.tflogger)
  # reproducibility
  # download training data from open datasets.
  try:
    training_data = datasets.FashionMNIST(
      root="data",
      train=True,
      download=False,
      transform=ToTensor(),
    )
    # Download test data from open datasets.
    test_data = datasets.FashionMNIST(
      root="data",
      train=False,
      download=False,
      transform=ToTensor(),
    )
  except:
    # not exists
    training_data = datasets.FashionMNIST(
      root="data",
      train=True,
      download=True,
      transform=ToTensor(),
    )
    
    # Download test data from open datasets.
    test_data = datasets.FashionMNIST(
      root="data",
      train=False,
      download=True,
      transform=ToTensor(),
    )
  
  training_data.data = training_data.data[:args.data_size]
  training_data.targets = training_data.targets[:args.data_size]
  nbatch = args.batch
  nepoch = args.epoch
  ninterval = args.interval
  itermax = args.itermax
  option_tr = args.option_tr
  hessian_window = args.hessian_window
  betas = (0.96, 0.99) if args.bool_decay else (0, 0)
  
  # get cpu or gpu device for training.
  device = "cuda" if torch.cuda.is_available() else "cpu"
  print(f"Using {device} device")
  
  loss_fn = F.cross_entropy
  methods = {
    'adam': torch.optim.Adam,
    'sgd1': torch.optim.SGD,
    'sgd2': torch.optim.SGD,
    'sgd3': torch.optim.SGD,
    'sgd4': torch.optim.SGD,
    'lbfgs': torch.optim.LBFGS,
    'drsom': DRSOMF,
    'drsomnd': DRSOMF3,
  }
  method_kwargs = {
    'adam':
      dict(lr=0.001, betas=(0.99, 0.999)),
    'sgd1':
      dict(lr=0.001, momentum=0.95),
    'sgd2':
      dict(lr=0.001, momentum=0.90),
    'sgd3':
      dict(lr=0.001, momentum=0.85),
    'sgd4':
      dict(lr=0.001, momentum=0.99),
    'lbfgs':
      dict(line_search_fn='strong_wolfe', max_iter=itermax),
    'drsom':
      dict(
        max_iter=itermax,
        thetas=(0.99, 0.999),
        option_tr=args.option_tr,
        hessian_window=hessian_window,
        beta1=args.drsom_beta1,
        beta2=args.drsom_beta2,
      ),
    'drsomnd':
      dict(
        max_iter=itermax,
        thetas=(0.99, 0.999),
        option_tr=args.option_tr,
        hessian_window=hessian_window,
        beta1=args.drsom_beta1,
        beta2=args.drsom_beta2,
      ),
    'drsomq':
      dict(
        max_iter=itermax,
        thetas=(0.99, 0.999),
        option_tr=args.option_tr,
        hessian_window=hessian_window,
        beta1=args.drsom_beta1,
        beta2=args.drsom_beta2,
      ),
  }
  
  pprint(method_kwargs)
  
  results = {}
  name = args.optim
  # model
  print("{:^10s}".format(name))
  if args.model == 'cnn':
    model = CNNModel().to(device)
  elif args.model == 'simple':
    model = VanillaNetwork().to(device)
  else:
    raise ValueError(f"unknown model {args.model}")
  
  if args.resume:
    ckpt = load_checkpoint(args.ckpt_name)
    start_epoch = ckpt['epoch']
    model.load_state_dict(ckpt['net'])
  else:
    ckpt = None
    start_epoch = 0
  # create data loaders.
  train_dataloader = DataLoader(training_data,
                                batch_size=nbatch,
                                generator=torch.Generator().manual_seed(1))
  test_dataloader = DataLoader(test_data,
                               batch_size=nbatch,
                               generator=torch.Generator().manual_seed(1))
  # start
  st = time.time()
  func = methods[name]
  func_kwargs = method_kwargs.get(name, {})
  optimizer = func(model.parameters(), **func_kwargs)
  # log name
  log_name = query_name(optimizer, name, args, ckpt)

  print(f"Using optimizer:\n {log_name}")
  for t in range(start_epoch, start_epoch + nepoch):
    try:
      print(f"epoch {t}")
      _, avg_loss, acc = train(train_dataloader, name, model, loss_fn,
                               optimizer, ninterval)
      if name.startswith('drsom') and (t + 1) % 5 == 0:
        optimizer.gammalb *= 1e3
        print("increase lower bound...")
      print("-------------------------------")
      
      # train loss
      writer.add_scalars("Loss/train", {f'{log_name}': avg_loss}, t)
      writer.add_scalars("Acc/train", {f'{log_name}': acc}, t)
      # test loss
      rt = test(test_dataloader, model, loss_fn)
      writer.add_scalars("Loss/test", {f"{log_name}": rt['avg_loss']}, t)
      writer.add_scalars("Acc/test", {f"{log_name}": rt['acc']}, t)
    except KeyboardInterrupt as e:
      print(f"Exiting at {t}")
      break
    if t % 10 == 0:
      print('Saving..')
      state = {
        'net': model.state_dict(),
        'epoch': t,
      }
      if not os.path.isdir('checkpoint'):
        os.mkdir('checkpoint')
      torch.save(state, os.path.join('checkpoint', f"{log_name}-{t}"))
  
  et = time.time()
  
  print("done!")
  subresult = {}
  subresult['info_train'] = test(train_dataloader, model, loss_fn)
  subresult['info_test'] = test(test_dataloader, model, loss_fn)
  subresult['time_train'] = et - st
  results[log_name] = subresult
  print(subresult)
  del model
  torch.cuda.empty_cache()
  
  print(json.dumps(results, indent=2))
