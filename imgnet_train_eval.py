import os
import time
import argparse
from tqdm import tqdm
from PIL import ImageFile
from datetime import datetime
from contextlib import ExitStack

import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
import torchvision.datasets as datasets

from nets.imgnet_vgg import vgg16
from nets.imgnet_alexnet import alexnet
from nets.imgnet_resnet import resnet18, resnet34, resnet50

from utils.utils import DisablePrint
from utils.summary import SummaryWriter
from utils.preprocessing import imgnet_transform

ImageFile.LOAD_TRUNCATED_IMAGES = True
torch.backends.cudnn.benchmark = True

# Training settings
parser = argparse.ArgumentParser(description='classification_baselines')

parser.add_argument('--dist', action='store_true')
parser.add_argument('--local_rank', type=int, default=0)

parser.add_argument('--root_dir', type=str, default='./')
parser.add_argument('--data_dir', type=str, default='./data')
parser.add_argument('--log_name', type=str, default='alexnet_baseline')
parser.add_argument('--pretrain', action='store_true', default=False)
parser.add_argument('--pretrain_dir', type=str, default='./ckpt/')

parser.add_argument('--lr', type=float, default=0.1)
parser.add_argument('--wd', type=float, default=5e-4)

parser.add_argument('--train_batch_size', type=int, default=256)
parser.add_argument('--test_batch_size', type=int, default=200)
parser.add_argument('--max_epochs', type=int, default=100)

parser.add_argument('--log_interval', type=int, default=10)
parser.add_argument('--gpus', type=str, default='0')
parser.add_argument('--num_workers', type=int, default=20)

cfg = parser.parse_args()

cfg.log_dir = os.path.join(cfg.root_dir, 'logs', cfg.log_name)
cfg.ckpt_dir = os.path.join(cfg.root_dir, 'ckpt', cfg.log_name)

os.makedirs(cfg.log_dir, exist_ok=True)
os.makedirs(cfg.ckpt_dir, exist_ok=True)

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # see issue #152
os.environ["CUDA_VISIBLE_DEVICES"] = cfg.gpus


def main():
  num_gpus = torch.cuda.device_count()
  if cfg.dist:
    device = torch.device('cuda:%d' % cfg.local_rank)
    torch.cuda.set_device(cfg.local_rank)
    dist.init_process_group(backend='nccl', init_method='env://',
                            world_size=num_gpus, rank=cfg.local_rank)
  else:
    device = torch.device('cuda')

  print('==> Preparing data ...')
  traindir = os.path.join(cfg.data_dir, 'train')
  train_dataset = datasets.ImageFolder(traindir, imgnet_transform(is_training=True))
  train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset,
                                                                  num_replicas=num_gpus,
                                                                  rank=cfg.local_rank)
  train_loader = torch.utils.data.DataLoader(train_dataset,
                                             batch_size=cfg.train_batch_size // num_gpus
                                             if cfg.dist else cfg.train_batch_size,
                                             shuffle=not cfg.dist,
                                             num_workers=cfg.num_workers,
                                             sampler=train_sampler if cfg.dist else None,
                                             pin_memory=True)

  evaldir = os.path.join(cfg.data_dir, 'val')
  val_dataset = datasets.ImageFolder(evaldir, imgnet_transform(is_training=False))
  val_loader = torch.utils.data.DataLoader(val_dataset,
                                           batch_size=cfg.test_batch_size,
                                           shuffle=False,
                                           num_workers=cfg.num_workers,
                                           pin_memory=True)

  # create model
  print('==> Building model ...')
  model = resnet50()
  model = model.to(device)
  if cfg.dist:
    model = nn.parallel.DistributedDataParallel(model,
                                                device_ids=[cfg.local_rank, ],
                                                output_device=cfg.local_rank)
  else:
    model = torch.nn.DataParallel(model)

  optimizer = torch.optim.SGD(model.parameters(), cfg.lr, momentum=0.9, weight_decay=cfg.wd)
  lr_schedulr = optim.lr_scheduler.MultiStepLR(optimizer, [30, 60, 90], 0.1)
  criterion = torch.nn.CrossEntropyLoss()

  summary_writer = SummaryWriter(cfg.log_dir)

  def train(epoch):
    # switch to train mode
    model.train()

    start_time = time.time()
    for batch_idx, (inputs, targets) in enumerate(train_loader):
      inputs, targets = inputs.to(device), targets.to(device)

      # compute output
      outputs = model(inputs)
      loss = criterion(outputs, targets)

      # compute gradient and do SGD step
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()

      if cfg.local_rank == 0 and batch_idx % cfg.log_interval == 0:
        step = len(train_loader) * epoch + batch_idx
        duration = time.time() - start_time

        print('%s epoch: %d step: %d cls_loss= %.5f (%d samples/sec)' %
              (datetime.now(), epoch, batch_idx, loss.item(),
               cfg.train_batch_size * cfg.log_interval / duration))

        start_time = time.time()
        summary_writer.add_scalar('cls_loss', loss.item(), step)
        summary_writer.add_scalar('learning rate', optimizer.param_groups[0]['lr'], step)

  def validate(epoch):
    # switch to evaluate mode
    model.eval()
    top1 = 0
    top5 = 0
    with torch.no_grad():
      for i, (inputs, targets) in tqdm(enumerate(val_loader)):
        inputs, targets = inputs.to(device), targets.to(device)

        # compute output
        output = model(inputs)

        # measure accuracy and record loss
        _, pred = output.data.topk(5, dim=1, largest=True, sorted=True)
        pred = pred.t()
        correct = pred.eq(targets.view(1, -1).expand_as(pred))

        top1 += correct[:1].view(-1).float().sum(0, keepdim=True).item()
        top5 += correct[:5].view(-1).float().sum(0, keepdim=True).item()

    top1 *= 100 / len(val_dataset)
    top5 *= 100 / len(val_dataset)
    print('%s Precision@1 ==> %.2f%%  Precision@1: %.2f%%\n' % (datetime.now(), top1, top5))

    summary_writer.add_scalar('Precision@1', top1, epoch)
    summary_writer.add_scalar('Precision@5', top5, epoch)
    return

  for epoch in range(cfg.max_epochs):
    train_sampler.set_epoch(epoch)
    train(epoch)
    validate(epoch)
    lr_schedulr.step(epoch)
    if cfg.local_rank == 0:
      torch.save(model.state_dict(), os.path.join(cfg.ckpt_dir, 'checkpoint.t7'))
      print('checkpoint saved to %s !' % os.path.join(cfg.ckpt_dir, 'checkpoint.t7'))

  summary_writer.close()


if __name__ == '__main__':
  with ExitStack() as stack:
    if cfg.local_rank != 0:
      stack.enter_context(DisablePrint())
    main()
