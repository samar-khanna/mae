# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# DeiT: https://github.com/facebookresearch/deit
# --------------------------------------------------------

from functools import partial

import torch
import torch.nn as nn

from timm.models.vision_transformer import PatchEmbed, Block

from util.pos_embed import get_2d_sincos_pos_embed, get_1d_sincos_pos_embed_from_grid


class MaskedAutoencoderChannelViT(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, spatial_mask=False,
                 channel_embed=256, embed_dim=1024, depth=24, num_heads=16,
                 decoder_channel_embed=128, decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False):
        super().__init__()

        self.in_c = in_chans
        self.spatial_mask = spatial_mask  # Whether to mask all channels of same spatial location

        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = PatchEmbed(img_size, patch_size, 1, embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim - channel_embed),
                                      requires_grad=False)  # fixed sin-cos embedding
        self.channel_embed = nn.Parameter(torch.zeros(1, in_chans, channel_embed), requires_grad=False)
        # self.enc_mask_token = nn.Parameter(torch.zeros(1, 1, embed_dim))

        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        # --------------------------------------------------------------------------

        # --------------------------------------------------------------------------
        # MAE decoder specifics
        self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))

        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, decoder_embed_dim - decoder_channel_embed),
                                              requires_grad=False)  # fixed sin-cos embedding
        # Extra channel for decoder to represent special place for cls token
        self.decoder_channel_embed = nn.Parameter(torch.zeros(1, in_chans + 1, decoder_channel_embed),
                                                  requires_grad=False)

        self.decoder_blocks = nn.ModuleList([
            Block(decoder_embed_dim, decoder_num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
            for i in range(decoder_depth)])

        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_pred = nn.Linear(decoder_embed_dim, patch_size**2, bias=True) # decoder to patch
        # --------------------------------------------------------------------------

        self.norm_pix_loss = norm_pix_loss

        self.initialize_weights()

    def initialize_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        channel_embed = get_1d_sincos_pos_embed_from_grid(self.channel_embed.shape[-1],
                                                          torch.arange(self.in_c).numpy())
        self.channel_embed.data.copy_(torch.from_numpy(channel_embed).float().unsqueeze(0))

        decoder_pos_embed = get_2d_sincos_pos_embed(self.decoder_pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)
        self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))

        dec_channel_embed = get_1d_sincos_pos_embed_from_grid(self.decoder_channel_embed.shape[-1],
                                                              torch.arange(self.in_c + 1).numpy())
        self.decoder_channel_embed.data.copy_(torch.from_numpy(dec_channel_embed).float().unsqueeze(0))

        # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed.proj.weight.data
        torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.cls_token, std=.02)
        torch.nn.init.normal_(self.mask_token, std=.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def patchify(self, imgs, p, c):
        """
        imgs: (N, C, H, W)
        p: Patch embed patch size
        c: Num channels
        x: (N, L*C, patch_size**2)
        """
        # p = self.patch_embed.patch_size[0]
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        # c = self.in_c
        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], c, h, p, w, p))
        x = torch.einsum('nchpwq->nchwpq', x)
        x = x.reshape(shape=(imgs.shape[0], c, h * w, p**2))
        x = x.view(imgs.shape[0], c*h*w, p**2)
        return x

    def unpatchify(self, x, p, c):
        """
        x: (N, L*C, patch_size**2)
        p: Patch embed patch size
        c: Num channels
        imgs: (N, C, H, W)
        """
        # c = self.in_c
        # p = self.patch_embed.patch_size[0]
        h = w = int((x.shape[1]/c)**.5)
        assert h * w * c == x.shape[1]
        
        x = x.reshape(shape=(x.shape[0], c, h, w, p, p))
        x = torch.einsum('nchwpq->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, h * p, h * p))
        return imgs

    def random_masking(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))

        noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]

        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore

    def forward_encoder(self, x, mask_ratio):
        # x is (N, C, H, W)
        b, c, h, w = x.shape

        # check if FMoW RGB is present
        has_rgb = torch.any(x[:, :3, :, :].view(b, -1), dim=-1)  # (N,)

        # Call to contiguous to ensure correct reshaping
        x_inp = x.view(b*c, 1, h, w)
        x_embed = self.patch_embed(x_inp).contiguous()  # (N*C, L, D)
        _, L, D = x_embed.shape
        x = x_embed.view(b, c, L, D)  # (N, C, L, D)

        # add channel embed
        channel_embed = self.channel_embed.unsqueeze(2)  # (1, c, 1, cD)
        pos_embed = self.pos_embed[:, 1:, :].unsqueeze(1)  # (1, 1, L, pD)

        # Channel embed same across (x,y) position, and pos embed same across channel (c)
        channel_embed = channel_embed.expand(-1, -1, pos_embed.shape[2], -1)  # (1, c, L, cD)
        pos_embed = pos_embed.expand(-1, channel_embed.shape[1], -1, -1)  # (1, c, L, pD)
        pos_channel = torch.cat((pos_embed, channel_embed), dim=-1)  # (1, c, L, D)

        # add pos embed w/o cls token
        x = x + pos_channel  # (N, c, L, D)

        if self.spatial_mask:
            # Mask spatial location across all channels (i.e. spatial location as either all/no channels)
            x = x.permute(0, 2, 1, 3).reshape(b, L, -1)  # (N, L, C*D)
            x, mask, ids_restore = self.random_masking(x, mask_ratio)  # (N, 0.25*L, C*D)
            x = x.view(b, x.shape[1], c, D).permute(0, 2, 1, 3).reshape(b, -1, D)  # (N, 0.25*C*L, D)
            mask = mask.repeat(1, c)  # (N, C*L)

            # ids_restore = ids_restore.repeat(1, c)  # (N, C*L)
            #
            # # Have to shift ids_restore for each channel by offset of L
            # offset = torch.arange(0, c*L, step=L, device=ids_restore.device).repeat_interleave(L)  # (C*L)
            # ids_restore += offset.view(1, -1)
        else:
            # Independently mask each channel (i.e. spatial location has subset of channels visible)
            x, mask, ids_restore = self.random_masking(x.view(b, -1, D), mask_ratio)  # (N, 0.25*C*L, D)

        # x_rgb = x[:, :3, :, :].view(b, -1, D)  # (N, 3*L, D)
        # x_sent = x[:, 3:, :, :].view(b, -1, D)  # (N, 13*L, D)
        #
        # # masking: length -> length * mask_ratio
        # x_rgb, mask_rgb, ids_restore_rgb = self.random_masking(x_rgb, mask_ratio)
        # x_sent, mask_sent, ids_restore_sent = self.random_masking(x_sent, mask_ratio)
        #
        # mask_rgb[~has_rgb, :] = 1  # Remove RGB if not there, 1 is remove, 0 is keep
        # x_rgb[~has_rgb, :, :] = self.enc_mask_token.expand(-1, x_rgb.shape[1], -1).view(x_rgb.shape[1], -1)
        # x = torch.cat((x_rgb, x_sent), dim=1)
        # mask = torch.cat((mask_rgb, mask_sent), dim=1)
        # ids_restore = torch.cat((ids_restore_rgb, ids_restore_sent))

        # append cls token
        # cls_token = self.cls_token + self.pos_embed[:, :1, :]  # (1, 1, D)
        cls_tokens = self.cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)  # (N, c*L + 1, D)

        # apply Transformer blocks
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        return x, mask, ids_restore

    def forward_decoder(self, x, ids_restore):
        # embed tokens
        x = self.decoder_embed(x)  # (N, c*0.25*L, D)

        # append mask tokens to sequence
        if self.spatial_mask:
            c = self.in_c
            N, L = ids_restore.shape

            x_ = x[:, 1:, :].view(N, c, -1, x.shape[2]).permute(0, 2, 1, 3)  # (N, 0.25*L, c, D)
            _, ml, _, D = x_.shape
            x_ = x_.reshape(N, ml, c*D)  # (N, 0.25*L, c*D)

            mask_tokens = self.mask_token.repeat(N, L - ml, c)
            x_ = torch.cat((x_, mask_tokens), dim=1)  # no cls token
            x_ = torch.gather(x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x_.shape[2]))  # (N, L, c*D)
            x_ = x_.view(N, L, c, D).permute(0, 2, 1, 3).reshape(N, -1, D)  # (N, c*L, D)
            x = torch.cat((x[:, :1, :], x_), dim=1)  # append cls token  (N, 1 + c*L, D)
        else:
            mask_tokens = self.mask_token.repeat(x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1)
            x_ = torch.cat([x[:, 1:, :], mask_tokens], dim=1)  # no cls token
            x_ = torch.gather(x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x.shape[2]))  # unshuffle
            x = torch.cat([x[:, :1, :], x_], dim=1)  # append cls token  (N, 1 + c*L, D)

        # add pos and channel embed
        channel_embed = self.decoder_channel_embed[:, :-1, :].unsqueeze(2)  # (1, c, 1, cD)
        pos_embed = self.decoder_pos_embed[:, 1:, :].unsqueeze(1)  # (1, 1, L, pD)

        channel_embed = channel_embed.expand(-1, -1, pos_embed.shape[2], -1)  # (1, c, L, cD)
        pos_embed = pos_embed.expand(-1, channel_embed.shape[1], -1, -1)  # (1, c, L, pD)
        pos_channel = torch.cat((pos_embed, channel_embed), dim=-1)  # (1, c, L, D)
        pos_channel = pos_channel.view(1, -1, pos_channel.shape[-1])  # (1, c*L, D)

        extra = torch.cat((self.decoder_pos_embed[:, :1, :],
                           self.decoder_channel_embed[:, -1:, :]), dim=-1)  # (1, 1, D)

        pos_channel = torch.cat((extra, pos_channel), dim=1)  # (1, c*L+1, D)
        x = x + pos_channel  # (N, c*L + 1, D)

        # apply Transformer blocks
        for blk in self.decoder_blocks:
            x = blk(x)
        x = self.decoder_norm(x)

        # predictor projection
        x = self.decoder_pred(x)

        # remove cls token
        x = x[:, 1:, :]

        return x

    def forward_loss(self, imgs, pred, mask):
        """
        imgs: [N, c, H, W]
        pred: [N, L*C, p*p]
        mask: [N, L], 0 is keep, 1 is remove, 
        """
        target = self.patchify(imgs, self.patch_embed.patch_size[0], self.in_c)  # (N, L*C, H*W)
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.e-6)**.5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch

        loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        return loss

    def forward(self, imgs, mask_ratio=0.75):
        latent, mask, ids_restore = self.forward_encoder(imgs, mask_ratio)
        pred = self.forward_decoder(latent, ids_restore)  # [N, L*C, p*p]
        loss = self.forward_loss(imgs, pred, mask)
        return loss, pred, mask


def mae_vit_base_patch16_dec512d8b(**kwargs):
    model = MaskedAutoencoderChannelViT(
        channel_embed=256, embed_dim=768, depth=12, num_heads=12,
        decoder_channel_embed=128, decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def mae_vit_large_patch16_dec512d8b(**kwargs):
    model = MaskedAutoencoderChannelViT(
        channel_embed=256, embed_dim=1024, depth=24, num_heads=16,
        decoder_channel_embed=128, decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def mae_vit_huge_patch14_dec512d8b(**kwargs):
    model = MaskedAutoencoderChannelViT(
        channel_embed=256, embed_dim=1280, depth=32, num_heads=16,
        decoder_channel_embed=128, decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


# set recommended archs
mae_vit_base_patch16 = mae_vit_base_patch16_dec512d8b  # decoder: 512 dim, 8 blocks
mae_vit_large_patch16 = mae_vit_large_patch16_dec512d8b  # decoder: 512 dim, 8 blocks
mae_vit_huge_patch14 = mae_vit_huge_patch14_dec512d8b  # decoder: 512 dim, 8 blocks
