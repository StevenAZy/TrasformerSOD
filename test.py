import cv2
import time
import torch
import torch.nn.functional as F
from torchvision import transforms
import pandas as pd
import numpy as np
import pdb, os, argparse
from dataset.dataloader import test_dataset, eval_Dataset, test_dataset_rgbd
from tqdm import tqdm
# from model.DPT import DPTSegmentationModel
from config import param as option
from model.get_model import get_model
from utils import sample_p_0, DotDict


def eval_mae(loader, cuda=True):
    avg_mae, img_num, total = 0.0, 0.0, 0.0
    with torch.no_grad():
        for pred, gt in loader:
            if cuda:
                pred, gt = pred.cuda(), gt.cuda()
            else:
                pred, gt = (pred), (gt)
            mae = torch.abs(pred - gt).mean()
            if mae == mae: # for Nan
                avg_mae += mae
                img_num += 1.0
        avg_mae /= img_num
    return avg_mae


class Tester():
    def __init__(self, option):
        self.option = option
        self.test_epoch_num = option['checkpoint'].split('/')[-1].split('_')[0]
        self.model, self.uncertainty_model = get_model(option)
        self.model.load_state_dict(torch.load(option['checkpoint']))
        self.model.train()
        if self.uncertainty_model is not None:
            self.uncertainty_model.load_state_dict(torch.load(option['checkpoint'].replace('generator', 'ebm_model')))
            self.uncertainty_model.eval()

    def prepare_test_params(self, dataset):
        save_path = os.path.join(option['eval_save_path'], self.test_epoch_num+'_epoch', dataset)
        print('[INFO]: Save_path is', save_path)
        if not os.path.exists(save_path): 
            os.makedirs(save_path)
        image_root = os.path.join(self.option['paths']['test_dataset_root'], 'Imgs', dataset)
        test_loader = test_dataset(image_root, option['testsize'])
        return {'save_path': save_path, 'test_loader': test_loader}

    def forward_a_sample(self, image, HH, WW):
        res = self.model.forward(img=image)[-1]  # Inference and get the last one of the output list
        res = F.upsample(res, size=[WW, HH], mode='bilinear', align_corners=False)
        res = res.sigmoid().data.cpu().numpy().squeeze()
        res = 255*(res - res.min()) / (res.max() - res.min() + 1e-8)
        
        return res

    def forward_a_sample_gan(self, image, HH, WW):
        z_noise = torch.randn(image.shape[0], 32).cuda()
        res = self.model.forward(img=image, z=z_noise)[-1]  # Inference and get the last one of the output list
        res = F.upsample(res, size=[WW, HH], mode='bilinear', align_corners=False)
        res = res.sigmoid().data.cpu().numpy().squeeze()
        res = 255*(res - res.min()) / (res.max() - res.min() + 1e-8)
        
        return res

    def forward_a_sample_ebm(self, image, HH, WW):
        ## Setup ebm params
        opt = DotDict()
        opt.ebm_out_dim = 1
        opt.ebm_middle_dim = 100
        opt.latent_dim = 32
        opt.e_init_sig = 1.0
        opt.e_l_steps = 5
        opt.e_l_step_size = 0.4
        opt.e_prior_sig = 1.0
        opt.g_l_steps = 5
        opt.g_llhd_sigma = 0.3
        opt.g_l_step_size = 0.1
        opt.e_energy_form = 'identity'
        ## Setup ebm params
        z_e_0 = sample_p_0(image, opt)
        ## sample langevin prior of z
        z_e_0 = torch.autograd.Variable(z_e_0)
        z = z_e_0.clone().detach()
        z.requires_grad = True
        for kk in range(opt.e_l_steps):
            en = self.uncertainty_model(z)
            z_grad = torch.autograd.grad(en.sum(), z)[0]
            z.data = z.data - 0.5 * opt.e_l_step_size * opt.e_l_step_size * (
                    z_grad + 1.0 / (opt.e_prior_sig * opt.e_prior_sig) * z.data)
            z.data += opt.e_l_step_size * torch.randn_like(z).data

        z_e_noise = z.detach()  ## z_
        res = self.model.forward(img=image, z=z_e_noise)[-1]
        res = F.upsample(res, size=[WW, HH], mode='bilinear', align_corners=False)
        res = res.sigmoid().data.cpu().numpy().squeeze()
        res = 255*(res - res.min()) / (res.max() - res.min() + 1e-8)
        
        return res

    def test_one_detaset(self, dataset):
        test_params = self.prepare_test_params(dataset)
        test_loader, save_path = test_params['test_loader'], test_params['save_path']

        time_list = []
        for i in tqdm(range(test_loader.size), desc=dataset):
            image, HH, WW, name = test_loader.load_data()
            image = image.cuda()
            torch.cuda.synchronize(); start = time.time()
            if self.option['uncer_method'] == 'vae' or self.option['uncer_method'] == 'abp' or self.option['uncer_method'] == 'basic':
                res = self.forward_a_sample(image, HH, WW)
            elif self.option['uncer_method'] == 'ebm':
                res = self.forward_a_sample_ebm(image, HH, WW)
            elif self.option['uncer_method'] == 'gan':
                res = self.forward_a_sample_gan(image, HH, WW)
            torch.cuda.synchronize(); end = time.time()
            time_list.append(end-start)

            cv2.imwrite(os.path.join(save_path, name), res)
            
        print('[INFO] Avg. Time used in this sequence: {:.4f}s'.format(np.mean(time_list)))


tester = Tester(option=option)
for dataset in option['datasets']:
    tester.test_one_detaset(dataset=dataset)

# Begin to evaluate the saved masks
mae_list = []
print('========== Begin to evaluate the saved masks ==========')
for dataset in tqdm(option['datasets']):
    if option['task'] == 'RGBD-SOD' or option['task'] == 'COD':
        gt_root = option['paths']['test_dataset_root'] + dataset + '/GT'
    else:
        gt_root = option['paths']['test_dataset_root'] + '/GT/' + dataset + '/'

    loader = eval_Dataset(os.path.join(option['eval_save_path'], '50_epoch', dataset), gt_root)
    mae = eval_mae(loader=loader, cuda=True)
    mae_list.append(mae.item())

print('--------------- Results ---------------')
results = np.array(mae_list)
results = np.reshape(results, [1, len(results)])
mae_table = pd.DataFrame(data=results, columns=option['datasets'])
# import pdb; pdb.set_trace()
with open(os.path.join(option['eval_save_path'], '50_epoch', 'results.csv'), 'w') as f:
    mae_table.to_csv(f, index=False, float_format="%.4f")
print(mae_table.to_string(index=False))
print('--------------- Results ---------------')
