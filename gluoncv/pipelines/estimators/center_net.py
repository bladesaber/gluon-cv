"""CenterNet Estimator"""
import os
import time
import warnings
from collections import OrderedDict

import mxnet as mx
from mxnet import gluon
from mxnet import autograd
from ...data.transforms.presets.center_net import CenterNetDefaultTrainTransform
from ...data.transforms.presets.center_net import CenterNetDefaultValTransform, get_post_transform
from ...data.batchify import Tuple, Stack, Pad
from ...utils.metrics.accuracy import Accuracy
from ...utils import LRScheduler, LRSequential
from ...model_zoo.center_net import get_center_net, get_base_network
from ...loss import MaskedL1Loss, HeatmapFocalLoss

from ..data.coco_detection import coco_detection, load_coco_detection
from .base_estimator import BaseEstimator, set_default, DotDict
from .common import train_hp, valid_hp

from sacred import Experiment, Ingredient

__all__ = ['CenterNetEstimator']

center_net = Ingredient('center_net')

@center_net.config
def center_net_default():
    base_network = 'dla34_deconv'    # base feature network
    heads = {
        'bias': -2.19,    # use bias = -log((1 - 0.1) / 0.1)
        'wh_outputs': 2,  # wh head channel
        'reg_outputs': 2,  # regression head channel
        'head_conv_channel': 64,  # additional conv channel
    }
    scale = 4.0  # output vs input scaling ratio, e.g., input_h // feature_h
    topk = 100  # topk detection results will be kept after inference
    root = os.path.expanduser(os.path.join('~', '.mxnet', 'models'))  # model zoo root dir
    wh_weight = 0.1  # Loss weight for width/height
    center_reg_weight = 1.0  # Center regression loss weight
    data_shape = (512, 512)

@train_hp.config
def update_train_config():
    gpus = (0, 1, 2, 3, 4, 5, 6, 7)
    pretrained_base = True  # whether load the imagenet pre-trained base
    batch_size = 32
    epochs = 140
    lr = 1.25e-4  # learning rate
    lr_decay = 0.1  # decay rate of learning rate.
    lr_decay_epoch = (90, 120)  # epochs at which learning rate decays
    lr_mode = 'step'  # learning rate scheduler mode. options are step, poly and cosine
    warmup_lr = 0.0  # starting warmup learning rate.
    warmup_epochs = 0  # number of warmup epochs


@valid_hp.config
def update_valid_config():
    flip_test = True  # use flip in validation test
    nms_thresh = 0  # 0 means disable
    nms_topk = 400  # pre nms topk
    post_nms = 100  # post nms topk

ex = Experiment('center_net_default',
                ingredients=[coco_detection, train_hp, valid_hp, center_net])

@ex.config
def default_configs():
    dataset = 'coco'


@set_default(ex)
class CenterNetEstimator(BaseEstimator):
    def __init__(self, config, logger=None):
        super(CenterNetEstimator, self).__init__(config, logger)

        # dataset
        if self._cfg.dataset == 'coco':
            train_dataset, val_dataset, val_metric = load_coco_detection(
                self._cfg.coco_detection.root,
                self._cfg.coco_detection.train_splits,
                self._cfg.coco_detection.valid_splits,
                self._cfg.coco_detection.valid_skip_empty,
                self._cfg.center_net.data_shape,
                self._cfg.coco_detection.cleanup,
                get_post_transform
            )
        else:
            raise NotImplementedError

        # network
        ctx = [mx.gpu(int(i)) for i in self._cfg.train_hp.gpus]
        ctx = ctx if ctx else [mx.cpu()]
        net_name = '_'.join(('center_net', self._cfg.center_net.base_network, self._cfg.dataset))
        heads = OrderedDict([
            ('heatmap', {'num_output': train_dataset.num_class, 'bias': self._cfg.center_net.heads.bias}),
            ('wh', {'num_output': self._cfg.center_net.heads.wh_outputs}),
            ('reg', {'num_output': self._cfg.center_net.heads.reg_outputs})])
        base_network = get_base_network(self._cfg.center_net.base_network, pretrained=self._cfg.train_hp.pretrained_base)
        net = get_center_net(self._cfg.center_net.base_network,
                             self._cfg.dataset,
                             base_network=base_network,
                             heads=heads,
                             head_conv_channel=self._cfg.center_net.heads.head_conv_channel,
                             classes=train_dataset.classes,
                             scale=self._cfg.center_net.scale,
                             topk=self._cfg.center_net.topk,
                             norm_layer=gluon.nn.BatchNorm)
        if self._cfg.train_hp.resume.strip():
            net.load_parameters(self._cfg.train_hp.resume.strip())
        else:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                net.initialize()

        # dataloader
        batch_size = self._cfg.train_hp.batch_size
        width, height = self._cfg.center_net.data_shape
        num_class = len(train_dataset.classes)
        batchify_fn = Tuple([Stack() for _ in range(6)])  # stack image, cls_targets, box_targets
        train_loader = gluon.data.DataLoader(
            train_dataset.transform(CenterNetDefaultTrainTransform(
                width, height, num_class=num_class, scale_factor=net.scale)),
            batch_size, True, batchify_fn=batchify_fn, last_batch='rollover',
            num_workers=self._cfg.train_hp.num_workers)
        val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))
        val_loader = gluon.data.DataLoader(
            val_dataset.transform(CenterNetDefaultValTransform(width, height)),
            self._cfg.valid_hp.batch_size, False, batchify_fn=val_batchify_fn, last_batch='keep',
            num_workers=self._cfg.valid_hp.num_workers)

        self._train_data = train_loader
        self._val_data = val_loader
        self._eval_metric = val_metric

        # trainer
        self._net = net
        self._net.collect_params().reset_ctx(ctx)
        lr_decay = float(self._cfg.train_hp.lr_decay)
        lr_steps = sorted(self._cfg.train_hp.lr_decay_epoch)
        lr_decay_epoch = [e - self._cfg.train_hp.warmup_epochs for e in lr_steps]
        num_batches = len(train_dataset) // self._cfg.train_hp.batch_size
        lr_scheduler = LRSequential([
            LRScheduler('linear', base_lr=0, target_lr=self._cfg.train_hp.lr,
                        nepochs=self._cfg.train_hp.warmup_epochs, iters_per_epoch=num_batches),
            LRScheduler(self._cfg.train_hp.lr_mode, base_lr=self._cfg.train_hp.LR,
                        nepochs=self._cfg.train_hp.epochs - self._cfg.train_hp.warmup_epochs,
                        iters_per_epoch=num_batches,
                        step_epoch=lr_decay_epoch,
                        step_factor=self._cfg.train_hp.lr_decay, power=2),
        ])

        for k, v in self._net.collect_params('.*bias').items():
            v.wd_mult = 0.0
        self._trainer = gluon.Trainer(
                    self._net.collect_params(), 'adam',
                    {'learning_rate': self._cfg.train_hp.lr, 'wd': self._cfg.train_hp.wd,
                     'lr_scheduler': lr_scheduler})

        self._save_prefix = os.path.join(self._logdir, net_name)
        self._best_map = 0

    def _fit(self):
        wh_loss = MaskedL1Loss(weight=self._cfg.center_net.wh_weight)
        heatmap_loss = HeatmapFocalLoss(from_logits=True)
        center_reg_loss = MaskedL1Loss(weight=self._cfg.center_net.center_reg_weight)
        heatmap_loss_metric = mx.metric.Loss('HeatmapFocal')
        wh_metric = mx.metric.Loss('WHL1')
        center_reg_metric = mx.metric.Loss('CenterRegL1')

        for epoch in range(self._cfg.train_hp.start_epoch, self._cfg.train_hp.epochs):
            wh_metric.reset()
            center_reg_metric.reset()
            heatmap_loss_metric.reset()
            tic = time.time()
            btic = time.time()
            self._net.hybridize()

            for i, batch in enumerate(self._train_data):
                split_data = [gluon.utils.split_and_load(batch[ind], ctx_list=ctx, batch_axis=0) for ind in range(6)]
                data, heatmap_targets, wh_targets, wh_masks, center_reg_targets, center_reg_masks = split_data
                batch_size = self._cfg.train_hp.batch_size
                with autograd.record():
                    sum_losses = []
                    heatmap_losses = []
                    wh_losses = []
                    center_reg_losses = []
                    wh_preds = []
                    center_reg_preds = []
                    for x, heatmap_target, wh_target, wh_mask, center_reg_target, center_reg_mask in zip(*split_data):
                        heatmap_pred, wh_pred, center_reg_pred = self._net(x)
                        wh_preds.append(wh_pred)
                        center_reg_preds.append(center_reg_pred)
                        wh_losses.append(wh_loss(wh_pred, wh_target, wh_mask))
                        center_reg_losses.append(center_reg_loss(center_reg_pred, center_reg_target, center_reg_mask))
                        heatmap_losses.append(heatmap_loss(heatmap_pred, heatmap_target))
                        curr_loss = heatmap_losses[-1]+ wh_losses[-1] + center_reg_losses[-1]
                        sum_losses.append(curr_loss)
                    autograd.backward(sum_losses)
                self._trainer.step(len(sum_losses))  # step with # gpus

                heatmap_loss_metric.update(0, heatmap_losses)
                wh_metric.update(0, wh_losses)
                center_reg_metric.update(0, center_reg_losses)
                if self._cfg.train_hp.log_interval and not (i + 1) % self._cfg.train_hp.log_interval:
                    name2, loss2 = wh_metric.get()
                    name3, loss3 = center_reg_metric.get()
                    name4, loss4 = heatmap_loss_metric.get()
                    self._log.info('[Epoch {}][Batch {}], Speed: {:.3f} samples/sec, LR={}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
                        epoch, i, batch_size/(time.time()-btic), trainer.learning_rate, name2, loss2, name3, loss3, name4, loss4))
                btic = time.time()

            name2, loss2 = wh_metric.get()
            name3, loss3 = center_reg_metric.get()
            name4, loss4 = heatmap_loss_metric.get()
            self._log.info('[Epoch {}] Training cost: {:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
                epoch, (time.time()-tic), name2, loss2, name3, loss3, name4, loss4))
            if (epoch % self._cfg.valid_hp.val_interval == 0) or \
               (self._cfg.train_hp.save_interval and epoch % self._cfg.train_hp.save_interval == 0) or \
               (epoch == self._cfg.train_hp.epochs - 1):
                # consider reduce the frequency of validation to save time
                map_name, mean_ap = validate()
                val_msg = '\n'.join(['{}={}'.format(k, v) for k, v in zip(map_name, mean_ap)])
                self._log.info('[Epoch {}] Validation: \n{}'.format(epoch, val_msg))
                current_map = float(mean_ap[-1])
            else:
                current_map = 0.
            save_params(current_map, epoch, self._cfg.train_hp.save_interval, self._save_prefix)

    def _evaluate(self):
        """Test on validation dataset."""
        self._eval_metric.reset()
        self._net.flip_test = self._cfg.valid_hp.flip_test
        mx.nd.waitall()
        self._net.hybridize()
        for batch in self._val_data:
            data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
            label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)
            det_bboxes = []
            det_ids = []
            det_scores = []
            gt_bboxes = []
            gt_ids = []
            gt_difficults = []
            for x, y in zip(data, label):
                # get prediction results
                ids, scores, bboxes = self._net(x)
                det_ids.append(ids)
                det_scores.append(scores)
                # clip to image size
                det_bboxes.append(bboxes.clip(0, batch[0].shape[2]))
                # split ground truths
                gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
                gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))
                gt_difficults.append(y.slice_axis(axis=-1, begin=5, end=6) if y.shape[-1] > 5 else None)

            # update metric
            self._eval_metric.update(det_bboxes, det_ids, det_scores, gt_bboxes, gt_ids, gt_difficults)
        return self._eval_metric.get()

    def _save_params(self, current_map, epoch, save_interval, prefix):
        current_map = float(current_map)
        if current_map > self._best_map:
            self._best_map = current_map
            self._net.save_parameters('{:s}_best.params'.format(prefix, epoch, current_map))
            with open(prefix+'_best_map.log', 'a') as f:
                f.write('{:04d}:\t{:.4f}\n'.format(epoch, current_map))
        if save_interval and epoch % save_interval == 0:
            self._net.save_parameters('{:s}_{:04d}_{:.4f}.params'.format(prefix, epoch, current_map))

@ex.automain
def main(_config, _log):
    # main is the commandline entry for user w/o coding
    c = CenterNetEstimator(_config, _log)
    c.fit()