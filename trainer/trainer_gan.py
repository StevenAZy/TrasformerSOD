import os
import pdb
import cv2
import time
import torch
import torch.nn.functional as F
from torch.autograd import Variable
from tqdm import tqdm
from config import param as option
from utils import AvgMeter, label_edge_prediction, visualize_list, make_dis_label
from loss.get_loss import cal_loss


class DotDict(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.__dict__ = self
    def allowDotting(self, state=True):
        if state:
            self.__dict__ = self
        else:
            self.__dict__ = dict()

CE = torch.nn.BCELoss()
def train_one_epoch(epoch, model_list, optimizer_list, train_loader, dataset_size, loss_fun):
    ## Setup abp params
    opt = DotDict()
    opt.latent_dim = option['latent_dim']
    opt.pred_label = 0
    opt.gt_label = 1
    ## Setup abp params

    generator, discriminator = model_list
    generator_optimizer, discriminator_optimizer = optimizer_list
    generator.train()
    if discriminator is not None:
        discriminator.train()
    loss_record, supervised_loss_record, dis_loss_record = AvgMeter(), AvgMeter(), AvgMeter()
    print('Learning Rate: {:.2e}'.format(generator_optimizer.param_groups[0]['lr']))
    progress_bar = tqdm(train_loader, desc='Epoch[{:03d}/{:03d}]'.format(epoch, option['epoch']))
    for i, pack in enumerate(progress_bar):
        for rate in option['size_rates']:
            generator_optimizer.zero_grad()
            if discriminator is not None:
                discriminator_optimizer.zero_grad()
            if len(pack) == 3:
                images, gts, depth, index = pack['image'].cuda(), pack['gt'].cuda(), None, pack['index']
            elif len(pack) == 4:
                images, gts, depth, index = pack['image'].cuda(), pack['gt'].cuda(), pack['depth'].cuda(), pack['index']
            # elif len(pack) == 4:
            #     images, gts, mask, gray = pack['image'].cuda(), pack['gt'].cuda(), pack['mask'].cuda(), pack['gray'].cuda()

            # multi-scale training samples
            trainsize = (int(round(option['trainsize']*rate/32)*32), int(round(option['trainsize']*rate/32)*32))
            if rate != 1:
                images = F.upsample(images, size=trainsize, mode='bilinear', align_corners=True)
                gts = F.upsample(gts, size=trainsize, mode='bilinear', align_corners=True)

            z_noise = torch.randn(images.shape[0], opt.latent_dim).cuda()
            pred = generator(img=images, z=z_noise)
            Dis_output = discriminator(torch.cat((images, pred[0].detach()), 1))
            up_size = (images.shape[2], images.shape[3])
            Dis_output = F.upsample(Dis_output, size=up_size, mode='bilinear', align_corners=True)
            
            loss_dis_output = CE(torch.sigmoid(Dis_output), make_dis_label(opt.gt_label, gts))
            supervised_loss = loss_fun(pred[0], gts)
            loss_all = supervised_loss + 0.1*loss_dis_output
            # import pdb; pdb.set_trace()
            loss_all.backward()
            generator_optimizer.step()

            # ## train discriminator
            dis_pred = torch.sigmoid(pred[0]).detach()
            Dis_output = discriminator(torch.cat((images, dis_pred), 1))
            Dis_target = discriminator(torch.cat((images, gts), 1))
            Dis_output = F.upsample(torch.sigmoid(Dis_output), size=up_size, mode='bilinear', align_corners=True)
            Dis_target = F.upsample(torch.sigmoid(Dis_target), size=up_size, mode='bilinear', align_corners=True)

            loss_dis_output = CE(Dis_output, make_dis_label(opt.pred_label, gts))
            loss_dis_target = CE(Dis_target, make_dis_label(opt.gt_label, gts))
            dis_loss = 0.5 * (loss_dis_output + loss_dis_target)
            dis_loss.backward()
            discriminator_optimizer.step()

            result_list = [torch.sigmoid(x) for x in pred]
            result_list.append(gts)
            visualize_list(result_list, option['log_path'])

            if rate == 1:
                loss_record.update(supervised_loss.data, option['batch_size'])
                supervised_loss_record.update((loss_all-supervised_loss).data, option['batch_size'])
                dis_loss_record.update(dis_loss.data, option['batch_size'])

        progress_bar.set_postfix(loss=f'{loss_record.show():.3f}|{supervised_loss_record.show():.3f}|{dis_loss_record.show():.3f}')

    return generator, loss_record
