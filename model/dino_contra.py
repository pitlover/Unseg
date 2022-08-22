from typing import Dict, Tuple, List
import torch
import torch.nn as nn
import torch.nn.functional as F  # noqa

from model.dino.dino_featurizer import DinoFeaturizer
from model.blocks.resnet import ResBlock
from model.quantizer import VectorQuantizer, EMAVectorQuantizer

import torchvision.transforms as transforms


class DINOContra(nn.Module):
    def __init__(self, cfg: dict):  # cfg["model"]
        super().__init__()
        self.cfg = cfg

        self.extractor = DinoFeaturizer(cfg["pretrained"])
        self.feat_dim = self.extractor.n_feats  # 384

        # -------- encoder -------- #
        num_enc_blocks = cfg["enc_num_blocks"]
        enc_proj = []
        for i in range(num_enc_blocks):
            enc_proj.append(ResBlock(self.feat_dim, self.feat_dim))
        self.enc_proj = nn.Sequential(*enc_proj)

        # -------- vq -------- #
        vq_num_codebooks = cfg["vq"]["num_codebooks"]
        vq_embed_dims = cfg["vq"]["embed_dims"]
        assert len(vq_num_codebooks) == len(vq_embed_dims)
        self.num_vq = len(vq_num_codebooks)
        self.beta = cfg["vq"]["beta"]
        self.normalize = cfg["vq"]["normalize"]
        self.vq_type = cfg["vq"]["vq_type"]
        self.use_restart = cfg["vq"].get("use_restart", False)
        self.use_gumbel = cfg["vq"].get("use_gumbel", False)
        self.jsd = JSD()

        vq_kwargs = dict(beta=self.beta, normalize=self.normalize,
                         use_restart=self.use_restart, use_gumbel=self.use_gumbel)

        if self.vq_type == "ema":
            vq_kwargs["decay"] = cfg["vq"]["decay"]
            vq_kwargs["eps"] = cfg["vq"]["eps"]
            vq_blocks = [
                EMAVectorQuantizer(vq_num_codebooks[i], vq_embed_dims[i], **vq_kwargs)
                for i in range(self.num_vq)
            ]
        elif self.vq_type == "param":
            vq_blocks = [
                VectorQuantizer(vq_num_codebooks[i], vq_embed_dims[i], **vq_kwargs)
                for i in range(self.num_vq)
            ]
        else:
            raise ValueError(f"Unsupported vq type {self.vq_type}.")
        self.vq_blocks = nn.ModuleList(vq_blocks)

        # -------- vq connections -------- #
        vq_input_proj = []
        for i in range(self.num_vq):
            vq_input_proj.append(nn.Conv2d(self.feat_dim, vq_embed_dims[i], 1, 1, 0))
        self.vq_input_proj = nn.ModuleList(vq_input_proj)

        vq_output_proj = []
        for i in range(self.num_vq - 1):
            vq_output_proj.append(nn.Sequential(
                nn.Conv2d(self.feat_dim + vq_embed_dims[i], self.feat_dim, 1, 1, 0),
                nn.ReLU(inplace=True)
            ))
        self.vq_output_proj = nn.ModuleList(vq_output_proj)

        self.vq_concat_proj = nn.Conv2d(sum(vq_embed_dims), self.feat_dim, 1, 1, 0)

        # -------- decoder -------- #
        num_dec_blocks = cfg["dec_num_blocks"]
        dec_proj = []
        for i in range(num_dec_blocks):
            dec_proj.append(ResBlock(self.feat_dim, self.feat_dim))
        self.dec_proj = nn.Sequential(*dec_proj)

    def _Augmentation(self, x: torch.Tensor):
        Augmentation = transforms.Compose([
            transforms.ColorJitter(brightness=.3, contrast=.3, saturation=.3, hue=.1),
            transforms.RandomGrayscale(.2),
            transforms.RandomApply([transforms.GaussianBlur((5, 5))])
        ])

        return Augmentation(x)

    def forward(self, img: torch.Tensor
                ) -> Tuple[torch.Tensor, List[torch.Tensor], Dict[str, torch.Tensor]]:
        img1 = self._Augmentation(img)
        dino_feat = self.extractor(img1)  # (b, 384, 28, 28) (b, d, h, w)
        feat = self.enc_proj(dino_feat)  # (b, 384, 28, 28)

        output = dict()
        feat_vqs = []

        for i in range(self.num_vq):
            feat_i = self.vq_input_proj[i](feat)
            feat_vq_i, vq_i_output, dis_prob = self.vq_blocks[i](feat_i)
            if i == 0:
                ori_dis_prob = dis_prob
            feat_vqs.append(feat_vq_i)

            for k, v in vq_i_output.items():
                output[f"vq{i}-{k}"] = v

            if i < self.num_vq - 1:
                feat_i = torch.cat([feat, feat_vq_i], dim=1)
                feat = self.vq_output_proj[i](feat_i)

        feat = torch.cat(feat_vqs, dim=1)
        feat = self.vq_concat_proj(feat)  # (b, 384, 28, 28)

        recon = self.dec_proj(feat)  # (b, 384, 28, 28)
        recon_loss = F.mse_loss(recon, dino_feat)

        output["recon-loss"] = recon_loss

        if self.training:
            # aug -> calculate loss only on stage1
            # TODO only photometric aug not geometric
            img2 = self._Augmentation(img)
            dino_feat2 = self.extractor(img2)
            feat2 = self.enc_proj(dino_feat2)

            feat2_i = self.vq_input_proj[0](feat2)
            feat2_vq_i, vq_i_output2, dis_prob2 = self.vq_blocks[0](feat2_i)
            output['contra-loss'] = self.jsd(ori_dis_prob, dis_prob2)

        return feat, feat_vqs, output

    def restart(self):
        for i in range(self.num_vq):
            self.vq_blocks[i].restart()


class JSD(nn.Module):
    def __init__(self):
        super().__init__()
        self.kl = nn.KLDivLoss(reduction='batchmean', log_target=True)

    def forward(self, p: torch.tensor, q: torch.tensor):
        '''

        :param p:    (bhw, K)
        :param q:    (bhw, K)
        :return:
        '''
        m = (0.5 * (p + q).add(1e-6)).log()
        return 0.5 * (self.kl(m, p.add(1e-6).log()) + self.kl(m, q.add(1e-6).log()))
