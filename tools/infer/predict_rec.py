# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import sys

__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.append(__dir__)
sys.path.append(os.path.abspath(os.path.join(__dir__, '../..')))

os.environ["FLAGS_allocator_strategy"] = 'auto_growth'

import cv2
import numpy as np
import math
import time
import traceback
import paddle

import tools.infer.utility as utility
from ppocr.postprocess import build_post_process
from ppocr.utils.logging import get_logger
from ppocr.utils.utility import get_image_file_list, check_and_read_gif
from single_sample_aug import sample_augmentation, split_aug

logger = get_logger()


class TextRecognizer(object):
    def __init__(self, args):
        self.rec_image_shape = [int(v) for v in args.rec_image_shape.split(",")]
        self.character_type = args.rec_char_type
        self.rec_batch_num = args.rec_batch_num
        self.rec_algorithm = args.rec_algorithm
        self.max_text_length = args.max_text_length
        postprocess_params = {
            'name': 'CTCLabelDecode',
            "character_type": args.rec_char_type,
            "character_dict_path": args.rec_char_dict_path,
            "use_space_char": args.use_space_char
        }
        if self.rec_algorithm == "SRN":
            postprocess_params = {
                'name': 'SRNLabelDecode',
                "character_type": args.rec_char_type,
                "character_dict_path": args.rec_char_dict_path,
                "use_space_char": args.use_space_char
            }
        elif self.rec_algorithm == "RARE":
            postprocess_params = {
                'name': 'AttnLabelDecode',
                "character_type": args.rec_char_type,
                "character_dict_path": args.rec_char_dict_path,
                "use_space_char": args.use_space_char
            }
        self.postprocess_op = build_post_process(postprocess_params)
        self.predictor, self.input_tensor, self.output_tensors = \
            utility.create_predictor(args, 'rec', logger)

    def resize_norm_img(self, img, max_wh_ratio):
        imgC, imgH, imgW = self.rec_image_shape
        assert imgC == img.shape[2]
        if self.character_type == "ch":
            imgW = int((32 * max_wh_ratio))
        h, w = img.shape[:2]
        ratio = w / float(h)
        if math.ceil(imgH * ratio) > imgW:
            resized_w = imgW
        else:
            resized_w = int(math.ceil(imgH * ratio))
        resized_image = cv2.resize(img, (resized_w, imgH))
        resized_image = resized_image.astype('float32')
        resized_image = resized_image.transpose((2, 0, 1)) / 255
        resized_image -= 0.5
        resized_image /= 0.5
        padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
        padding_im[:, :, 0:resized_w] = resized_image
        return padding_im

    def resize_norm_img_srn(self, img, image_shape):
        imgC, imgH, imgW = image_shape

        img_black = np.zeros((imgH, imgW))
        im_hei = img.shape[0]
        im_wid = img.shape[1]

        if im_wid <= im_hei * 1:
            img_new = cv2.resize(img, (imgH * 1, imgH))
        elif im_wid <= im_hei * 2:
            img_new = cv2.resize(img, (imgH * 2, imgH))
        elif im_wid <= im_hei * 3:
            img_new = cv2.resize(img, (imgH * 3, imgH))
        else:
            img_new = cv2.resize(img, (imgW, imgH))

        img_np = np.asarray(img_new)
        img_np = cv2.cvtColor(img_np, cv2.COLOR_BGR2GRAY)
        img_black[:, 0:img_np.shape[1]] = img_np
        img_black = img_black[:, :, np.newaxis]

        row, col, c = img_black.shape
        c = 1

        return np.reshape(img_black, (c, row, col)).astype(np.float32)

    def srn_other_inputs(self, image_shape, num_heads, max_text_length):

        imgC, imgH, imgW = image_shape
        feature_dim = int((imgH / 8) * (imgW / 8))

        encoder_word_pos = np.array(range(0, feature_dim)).reshape(
            (feature_dim, 1)).astype('int64')
        gsrm_word_pos = np.array(range(0, max_text_length)).reshape(
            (max_text_length, 1)).astype('int64')

        gsrm_attn_bias_data = np.ones((1, max_text_length, max_text_length))
        gsrm_slf_attn_bias1 = np.triu(gsrm_attn_bias_data, 1).reshape(
            [-1, 1, max_text_length, max_text_length])
        gsrm_slf_attn_bias1 = np.tile(
            gsrm_slf_attn_bias1,
            [1, num_heads, 1, 1]).astype('float32') * [-1e9]

        gsrm_slf_attn_bias2 = np.tril(gsrm_attn_bias_data, -1).reshape(
            [-1, 1, max_text_length, max_text_length])
        gsrm_slf_attn_bias2 = np.tile(
            gsrm_slf_attn_bias2,
            [1, num_heads, 1, 1]).astype('float32') * [-1e9]

        encoder_word_pos = encoder_word_pos[np.newaxis, :]
        gsrm_word_pos = gsrm_word_pos[np.newaxis, :]

        return [
            encoder_word_pos, gsrm_word_pos, gsrm_slf_attn_bias1,
            gsrm_slf_attn_bias2
        ]

    def process_image_srn(self, img, image_shape, num_heads, max_text_length):
        norm_img = self.resize_norm_img_srn(img, image_shape)
        norm_img = norm_img[np.newaxis, :]

        [encoder_word_pos, gsrm_word_pos, gsrm_slf_attn_bias1, gsrm_slf_attn_bias2] = \
            self.srn_other_inputs(image_shape, num_heads, max_text_length)

        gsrm_slf_attn_bias1 = gsrm_slf_attn_bias1.astype(np.float32)
        gsrm_slf_attn_bias2 = gsrm_slf_attn_bias2.astype(np.float32)
        encoder_word_pos = encoder_word_pos.astype(np.int64)
        gsrm_word_pos = gsrm_word_pos.astype(np.int64)

        return (norm_img, encoder_word_pos, gsrm_word_pos, gsrm_slf_attn_bias1,
                gsrm_slf_attn_bias2)

    def __call__(self, img_list):
        img_num = len(img_list)
        # Calculate the aspect ratio of all text bars
        width_list = []
        for img in img_list:
            width_list.append(img.shape[1] / float(img.shape[0]))
        # Sorting can speed up the recognition process
        indices = np.argsort(np.array(width_list))

        # rec_res = []
        rec_res = [['', 0.0]] * img_num
        batch_num = self.rec_batch_num
        elapse = 0
        for beg_img_no in range(0, img_num, batch_num):
            end_img_no = min(img_num, beg_img_no + batch_num)
            norm_img_batch = []
            max_wh_ratio = 0
            # for ino in range(beg_img_no, end_img_no):
            #     # h, w = img_list[ino].shape[0:2]
            #     h, w = img_list[indices[ino]].shape[0:2]
            #     wh_ratio = w * 1.0 / h
            #     max_wh_ratio = max(max_wh_ratio, wh_ratio)
            '''
            TODO fix the ratio
            '''
            max_wh_ratio = int(self.rec_image_shape[2] / self.rec_image_shape[1])
            for ino in range(beg_img_no, end_img_no):
                if self.rec_algorithm != "SRN":
                    norm_img = self.resize_norm_img(img_list[indices[ino]], max_wh_ratio)
                    norm_img = norm_img[np.newaxis, :]
                    norm_img_batch.append(norm_img)
                else:
                    norm_img = self.process_image_srn(img_list[indices[ino]],
                                                      self.rec_image_shape, 8,
                                                      self.max_text_length)
                    encoder_word_pos_list = []
                    gsrm_word_pos_list = []
                    gsrm_slf_attn_bias1_list = []
                    gsrm_slf_attn_bias2_list = []
                    encoder_word_pos_list.append(norm_img[1])
                    gsrm_word_pos_list.append(norm_img[2])
                    gsrm_slf_attn_bias1_list.append(norm_img[3])
                    gsrm_slf_attn_bias2_list.append(norm_img[4])
                    norm_img_batch.append(norm_img[0])
            norm_img_batch = np.concatenate(norm_img_batch)
            norm_img_batch = norm_img_batch.copy()

            if self.rec_algorithm == "SRN":
                starttime = time.time()
                encoder_word_pos_list = np.concatenate(encoder_word_pos_list)
                gsrm_word_pos_list = np.concatenate(gsrm_word_pos_list)
                gsrm_slf_attn_bias1_list = np.concatenate(
                    gsrm_slf_attn_bias1_list)
                gsrm_slf_attn_bias2_list = np.concatenate(
                    gsrm_slf_attn_bias2_list)

                inputs = [
                    norm_img_batch,
                    encoder_word_pos_list,
                    gsrm_word_pos_list,
                    gsrm_slf_attn_bias1_list,
                    gsrm_slf_attn_bias2_list,
                ]
                input_names = self.predictor.get_input_names()
                for i in range(len(input_names)):
                    input_tensor = self.predictor.get_input_handle(input_names[
                        i])
                    input_tensor.copy_from_cpu(inputs[i])
                self.predictor.run()
                outputs = []
                for output_tensor in self.output_tensors:
                    output = output_tensor.copy_to_cpu()
                    outputs.append(output)
                preds = {"predict": outputs[2]}
            else:
                starttime = time.time()
                self.input_tensor.copy_from_cpu(norm_img_batch)
                self.predictor.run()

                outputs = []
                for output_tensor in self.output_tensors:
                    output = output_tensor.copy_to_cpu()
                    outputs.append(output)
                preds = outputs[0]
            self.predictor.try_shrink_memory()
            rec_result = self.postprocess_op(preds)
            for rno in range(len(rec_result)):
                rec_res[indices[beg_img_no + rno]] = rec_result[rno]
            elapse += time.time() - starttime
        return rec_res, elapse


def main(args):
    image_file_list = get_image_file_list(args.image_dir)
    text_recognizer = TextRecognizer(args)
    total_run_time = 0.0
    total_images_num = 0
    valid_image_file_list = []
    img_list = []
    lines = []
    details = []
    for idx, image_file in enumerate(image_file_list):
        img, flag = check_and_read_gif(image_file)
        # # if not flag:
        # #     img = cv2.imread(image_file)
        # # if img is None:
        # #     logger.info("error in loading image:{}".format(image_file))
        # #     continue
        # # valid_image_file_list.append(image_file)
        # # img_list.append(img)
        if not flag:
            img = cv2.imread(image_file)
            # '''
            # TODO data augmentation
            # '''
            # image_files_, imgs_ = [image_file] * 8, sample_augmentation(img)
            # image_files_, imgs_ = split_aug(img, image_file)
        if img is None:
            logger.info("error in loading image:{}".format(image_file))
            continue
        # valid_image_file_list.append(image_file)
        # img_list.append(img)
        valid_image_file_list += [image_file]
        img_list += [img]
        
        if len(img_list) >= args.rec_batch_num or idx == len(
                image_file_list) - 1:
            try:
                rec_res, predict_time = text_recognizer(img_list)
                total_run_time += predict_time
            except:
                logger.info(traceback.format_exc())
                logger.info(
                    "ERROR!!!! \n"
                    "Please read the FAQ：https://github.com/PaddlePaddle/PaddleOCR#faq \n"
                    "If your model has tps module:  "
                    "TPS does not support variable shape.\n"
                    "Please set --rec_image_shape='3,32,100' and --rec_char_type='en' "
                )
                exit()
            # lines = []
            for ino in range(len(img_list)):
                imgname = valid_image_file_list[ino].split('/')[-1]
                lines.append(imgname+'\t'+rec_res[ino][0]+'\n')
                # import ipdb; ipdb.set_trace()
                # details.append(imgname + '\t' + rec_res[ino][0] + '\t' + str(rec_res[ino][1]) + '\t' + ','.join([str(t) for t in rec_res[ino][2]]) + '\t' + ','.join([str(t) for t in rec_res[ino][3].tolist()]) + '\n')
                details.append(imgname + '\t' + rec_res[ino][0]  + '\t' + ','.join(
                    [str(t) for t in rec_res[ino][3].tolist()]) + '\n')


                # import ipdb; ipdb.set_trace()
                logger.info("Predicts of {}:{}".format(valid_image_file_list[
                    ino], rec_res[ino][:2]))
        
            total_images_num += len(valid_image_file_list)
            valid_image_file_list = []
            img_list = []

    with open(args.save_res_path, 'w') as f:
        print('length of lines is:', len(lines))
        f.writelines(lines)
    with open('details.txt', 'w') as f:
        f.writelines(details)
    logger.info("Total predict time for {} images, cost: {:.3f}".format(
        total_images_num, total_run_time))


if __name__ == "__main__":
    main(utility.parse_args())
