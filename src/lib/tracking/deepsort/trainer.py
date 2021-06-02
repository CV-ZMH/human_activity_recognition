# -*- coding: utf-8 -*-
import os
from collections import namedtuple, OrderedDict
from tqdm import tqdm

import torch
from torchvision import transforms
from torch.backends import cudnn
from torch.utils.data import DataLoader

from runner import Runner, RunBuilder
from datasets import get_transforms, get_dataset, get_gaussian_mask
from models.get_extractor import get_model

class Trainer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.save_root = os.path.join(cfg.weight_root, f'{cfg.reid_net}_{cfg.dataset.name}')
        os.makedirs(self.save_root, exist_ok=True)

        self.start_epoch = 0
        self.lr = 0
        self.best_acc = 0.
        self.train_loader = None
        self.val_loader = None
        self.num_classes = 0

        self.device = self.setup_device(cfg.GPU)
        # get runs for hyperparameters
        self.runs = RunBuilder.get_runs(cfg.params)
        # get dataloaders
        self.load_data(cfg.dataset.root, cfg.reid_net)

    @staticmethod
    def setup_device(device_id):
        device = torch.device(f'cuda:{cfg.GPU}' if device_id >= 0
                      and torch.cuda.is_available() else 'cpu')
        # setup cudnn settings
        if device.type != 'cpu':
            cudnn.benchmark = True
            cudnn.deterministic = True
            cudnn.enabled = True
        print(f'[INFO] Running on device -> {device}')
        return device

    def load_model(self, reid_net, pretrained=''):
        """Build model and load pretrained model.
        Also initialize training variables."""
        net = get_model(reid_net, reid=False, num_classes=self.num_classes)
        if os.path.isfile(pretrained):
            print(f'[INFO] Loading pretrained model : {pretrained}')
            checkpoint = torch.load(pretrained, map_location=lambda storage, loc: storage)
            net.load_state_dict(checkpoint['net_dict'])
            self.start_epoch = checkpoint['epoch']
            self.best_acc = checkpoint['acc']
            self.lr = checkpoint['lr']
        return net.to(self.device)

    def load_data(self, root, reid_net):
        "Get dataloaders of train and validation set."
        cfg_loader = self.cfg.dataset
        tfms = get_transforms(*cfg_loader.size) # this transform doesn't include normalize
        # load training set
        train_dataset = get_dataset(
            self.cfg.reid_net,
            cfg_loader.name,
            root=root,
            mode='val',
            tfms=tfms)
        self.train_loader = DataLoader(
            train_dataset, shuffle=False,
            batch_size=cfg_loader.batch_size,
            num_workers=cfg_loader.workers)

        # load validation set
        val_dataset = get_dataset(
            self.cfg.reid_net,
            cfg_loader.name,
            root=root,
            mode='val',
            tfms=tfms)
        self.val_loader = DataLoader(
            val_dataset, shuffle=False,
            batch_size=cfg_loader.batch_size,
            num_workers=cfg_loader.workers)

        self.num_classes = train_dataset.num_classes

    def save_checkpoint(self, filename, net, epoch, lr):
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        print(f'[INFO] Saving checkpoint to {filename}')
        checkpoint = {
            'net_dict' : net.state_dict,
            'acc' : self.best_acc,
            'epoch' : epoch,
            'lr' : lr
            }
        torch.save(checkpoint, filename)

    def train(self):
        # initiate runner
        tb_folder = os.path.join(self.save_root, 'runs')
        runner = Runner(tb_folder=tb_folder)

        # normalization with custom dataset's mean and std
        norm = transforms.Normalize(self.cfg.dataset.mean, self.cfg.dataset.std)
        for run in self.runs:
            print(run)
            # get network
            self.lr = run.lr
            net = self.load_model(self.cfg.reid_net, self.cfg.train.pretrained)
            # init tensorboard and add the data
            runner.begin_run(run, net, self.train_loader, self.val_loader)
            # loss
            criterion = torch.nn.CrossEntropyLoss()
            # optimizer
            if run.optim == 'SGD':
                optimizer = torch.optim.SGD(
                    net.parameters(), self.lr,
                    momentum=0.9, weight_decay=5e-4
                    )
            elif run.optim == 'Adam':
                optimizer = torch.optim.Adam(net.parameters(), self.lr)

            # scheduler
            if getattr(run, 'reduce_lr', False):
                scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                    optimizer, factor=0.1, patience=10, verbose=True
                    )

            # epoch loop
            for epoch in range(self.start_epoch, self.cfg.train.epoch):
                # track training information of that epoch
                runner.begin_epoch()

                # training loop
                tq_template = "Epoch: [{}/{}] Iter: [{}/{}] LR: {} Loss: {:.8f}"
                # tq = tqdm(enumerate(self.train_loader), total=len(self.train_loader),
                #           desc=tq_template.format(
                #               epoch+1, self.cfg.train.epoch,
                #               0, len(self.train_loader),
                #               self.lr, 0)
                #           )
                images, labels = next(iter(self.train_loader))
                # for i_iter, (images, labels) in tq:
                tq = tqdm(range(1))
                for i_iter in tq:
                    images, labels = images.to(self.device), labels.to(self.device)

                    # muliply with mask and then normalize
                    if getattr(run, 'gaussian_mask', False):
                        mask = get_gaussian_mask(*self.cfg.dataset.size) #
                        images = images * mask.to(self.device)

                    tfms_images = norm(images)
                    preds = net(tfms_images)
                    loss = criterion(preds, labels)
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    tq.set_description(tq_template.format(
                            epoch+1, self.cfg.train.epoch,
                            i_iter, len(self.train_loader),
                            optimizer.param_groups[0]['lr'],
                            loss.item()
                        ))

                    runner.track_loss(loss, tfms_images)
                    runner.track_num_correct(preds, labels)
                    # break
                runner.add_tb_data(status='train')
                checkpoint_file = os.path.join(
                        self.save_root,
                        'checkpoints',
                        f'epoch_{epoch}-{self.best_acc}.pth')

                if epoch+1 % self.cfg.train.save_interval == 0:
                    self.save_checkpoint(checkpoint_file, net, epoch, optimizer.param_groups[0]['lr'])
                torch.cuda.empty_cache()

                # validation loop
                # if epoch+1 % self.cfg.train.val_interval == 0:
                if epoch+1 == self.cfg.train.epoch:
                    print('Validation ...')
                    net.eval()
                    with torch.no_grad():
                        for images, labels in tqdm(self.val_loader):
                            images, labels = images.to(self.device), labels.to(self.device)
                            # muliply with mask and then normalize
                            if getattr(run, 'gaussian_mask', False):
                                mask = get_gaussian_mask(*self.cfg.dataset.size) #
                                images = images * mask.to(self.device)

                            tfms_images = norm(images)
                            preds = net(tfms_images )
                            loss = criterion(preds, labels)
                            runner.track_loss(loss, tfms_images)
                            runner.track_num_correct(preds, labels)
                            # break
                        runner.add_tb_data(status='val')
                    print(f"Val Loss: {runner.val_loss:.3f} Best Acc: {self.best_acc}")

                    if runner.val_accuracy > self.best_acc:
                        self.best_acc = runner.val_accuracy
                        self.save_checkpoint(checkpoint_file, net, epoch, optimizer.param_groups[0]['lr'])

                # add all epoch record data in tensorboard
                if getattr(run, 'reduce_lr', False):
                    scheduler.step()
                runner.end_epoch(images=images)
            runner.end_run()
            self.best_acc = 0
        result_file = os.path.join(self.save_root, 'train_result')
        runner.save(result_file)


if __name__ == '__main__':
    import sys
    root = '../../../'
    sys.path.insert(0, root)
    from lib.utils import parser
    cfg_file = os.path.join(root, '../configs/training_reid.yaml')
    cfg = parser.YamlParser(config_file=cfg_file)
    trainer = Trainer(cfg)
    trainer.train()