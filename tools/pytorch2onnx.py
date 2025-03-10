import argparse
import os.path as osp
from functools import partial

import mmcv
import numpy as np
import onnx
import onnxruntime as rt
import torch
from mmcv.runner import load_checkpoint

from mmdet.models import build_detector

'''
try:
    from mmcv.onnx.symbolic import register_extra_symbolics
except ModuleNotFoundError:
    raise NotImplementedError('please update mmcv to version>=v1.0.4')
'''


def pytorch2onnx(model,
                 input_img,
                 input_shape,
                 opset_version=11,
                 show=False,
                 output_file='tmp.onnx',
                 verify=False,
                 do_simplify=False,
                 normalize_cfg=None):
    model.cuda().eval()
    # read image
    one_img = mmcv.imread(input_img)
    if normalize_cfg:
        one_img = mmcv.imnormalize(one_img, normalize_cfg['mean'],
                                   normalize_cfg['std'])
    one_img = mmcv.imresize(one_img, input_shape[2:]).transpose(2, 0, 1)
    one_img = torch.from_numpy(one_img).unsqueeze(0).float().cuda()
    (_, C, H, W) = input_shape
    one_meta = {
        'img_shape': (H, W, C),
        'ori_shape': (H, W, C),
        'pad_shape': (H, W, C),
        'filename': '<demo>.png',
        'scale_factor': 1.0,
        'flip': False
    }
    # onnx.export does not support kwargs
    origin_forward = model.forward
    model.forward = partial(
        model.forward, img_metas=[[one_meta]], return_loss=False, rescale=False, eval=True)
    # pytorch has some bug in pytorch1.3, we have to fix it
    # by replacing these existing op
    # register_extra_symbolics(opset_version)

    output_names = ['pan', 'cat']
    input_name = 'input'

    torch.onnx.export(
        model, ([one_img]),
        output_file,
        input_names=[input_name],
        output_names=output_names,
        export_params=True,
        do_constant_folding=True,
        verbose=show,
        opset_version=opset_version)
    model.forward = origin_forward

    if do_simplify:
        import onnxsim

        input_dic = {'input': one_img.detach().cpu().numpy()}
        onnx_model = onnx.load(output_file)
        model_simp, check = onnxsim.simplify(onnx_model, input_data=input_dic)
        assert check, "Simplified ONNX model could not be validated"

        removed_initializers = []
        for initializer in model_simp.graph.initializer:
            if initializer.name in ['1218', '1081', '971', '861', '758', '756']:
                removed_initializers.append(initializer)
        for initializer in removed_initializers:
            model_simp.graph.initializer.remove(initializer)
            
        onnx.save(model_simp, output_file)
        
    print(f'Successfully exported ONNX model: {output_file}')

    if verify:
        # check by onnx
        onnx_model = onnx.load(output_file)
        onnx.checker.check_model(onnx_model)

        # check the numerical value
        # get pytorch output
        pytorch_result = model([one_img], [[one_meta]], return_loss=False, rescale=False, eval=True)
        pytorch_pan_pred, pytorch_cat_pred = pytorch_result[0]
        pytorch_pan_pred, pytorch_cat_pred = pytorch_pan_pred.numpy(), pytorch_cat_pred.numpy()

        # get onnx output
        input_all = [node.name for node in onnx_model.graph.input]
        input_initializer = [
            node.name for node in onnx_model.graph.initializer
        ]
        net_feed_input = list(set(input_all) - set(input_initializer))
        assert (len(net_feed_input) == 1)
        sess = rt.InferenceSession(output_file)
        pan_pred, cat_pred = sess.run(
            None, {net_feed_input[0]: one_img.detach().cpu().numpy()})
        # only compare a part of result
        assert np.allclose(
            pytorch_pan_pred, pan_pred
        ) and np.allclose(
            pytorch_cat_pred, cat_pred
        ), 'The outputs are different between Pytorch and ONNX'
        print('The numerical values are same between Pytorch and ONNX')


def parse_args():
    parser = argparse.ArgumentParser(
        description='Convert MMDetection models to ONNX')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument('--input-img', type=str, help='Images for input')
    parser.add_argument('--show', action='store_true', help='show onnx graph')
    parser.add_argument('--output-file', type=str, default='tmp.onnx')
    parser.add_argument('--opset-version', type=int, default=11)
    parser.add_argument(
        '--verify',
        action='store_true',
        help='verify the onnx model output against pytorch output')
    parser.add_argument(
        '--simplify',
        action='store_true',
        help='Whether to simplify onnx model.')
    parser.add_argument(
        '--shape',
        type=int,
        nargs='+',
        default=[384, 768],
        help='input image size')
    parser.add_argument(
        '--mean',
        type=int,
        nargs='+',
        default=[123.675, 116.28, 103.53],
        help='mean value used for preprocess input data')
    parser.add_argument(
        '--std',
        type=int,
        nargs='+',
        default=[58.395, 57.12, 57.375],
        help='variance value used for preprocess input data')
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = parse_args()

    assert args.opset_version == 11, 'MMDet only support opset 11 now'

    if not args.input_img:
        args.input_img = osp.join(
            osp.dirname(__file__), '../tests/data/color.jpg')

    if len(args.shape) == 1:
        input_shape = (1, 3, args.shape[0], args.shape[0])
    elif len(args.shape) == 2:
        input_shape = (1, 3) + tuple(args.shape)
    else:
        raise ValueError('invalid input shape')

    assert len(args.mean) == 3
    assert len(args.std) == 3

    normalize_cfg = {
        'mean': np.array(args.mean, dtype=np.float32),
        'std': np.array(args.std, dtype=np.float32)
    }

    cfg = mmcv.Config.fromfile(args.config)
    cfg.model.pretrained = None
    cfg.data.test.test_mode = True

    # build the model
    model = build_detector(cfg.model, train_cfg=None, test_cfg=cfg.test_cfg)
    checkpoint = load_checkpoint(model, args.checkpoint, map_location='cuda')

    # conver model to onnx file
    pytorch2onnx(
        model,
        args.input_img,
        input_shape,
        opset_version=args.opset_version,
        show=args.show,
        output_file=args.output_file,
        verify=args.verify,
        do_simplify=args.simplify,
        normalize_cfg=normalize_cfg)
