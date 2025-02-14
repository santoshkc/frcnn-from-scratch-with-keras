from __future__ import division
import os
import cv2
import numpy as np
import sys
import pickle
from optparse import OptionParser
import time

#os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

from keras import backend as K
from tensorflow.keras.optimizers import Adam, SGD, RMSprop
from tensorflow.keras.layers import Input
from tensorflow.keras.models import Model, load_model
from keras_frcnn import config, data_generators
from keras_frcnn import losses as losses
import keras_frcnn.roi_helpers as roi_helpers
from tensorflow.keras.utils import Progbar

import os


if 'tensorflow' == K.backend():
	import tensorflow.compat.v1 as tf
	tf.disable_v2_behavior()

from tensorflow.compat.v1.keras.backend import set_session
from keras.applications.mobilenet import preprocess_input

#from tensorflow.compat.v1 import ConfigProto
#from tensorflow.compat.v1 import InteractiveSession
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
session = tf.InteractiveSession(config=config)

sys.setrecursionlimit(40000)

parser = OptionParser()

parser.add_option("-p", "--path", dest="test_path", help="Path to test data.")
parser.add_option("-n", "--num_rois", type="int", dest="num_rois",
				help="Number of ROIs per iteration. Higher means more memory use.", default=32)
parser.add_option("--config_filename", dest="config_filename", help=
				"Location to read the metadata related to the training (generated when training).",
				default="config.pickle")
parser.add_option("--network", dest="network", help="Base network to use. Supports vgg or resnet50.", default='resnet50')
parser.add_option("--write", dest="write", help="to write out the image with detections or not.", action='store_true')
parser.add_option("--load", dest="load", help="specify model path.", default=None)
(options, args) = parser.parse_args()

if not options.test_path:   # if filename is not given
	parser.error('Error: path to test data must be specified. Pass --path to command line')


config_output_filename = options.config_filename

with open(config_output_filename, 'rb') as f_in:
	C = pickle.load(f_in)

# we will use resnet. may change to vgg
if options.network == 'vgg' or options.network == 'vgg16':
	C.network = 'vgg16'
	from keras_frcnn import vgg as nn
elif options.network == 'resnet50':
	from keras_frcnn import resnet as nn
	C.network = 'resnet50'
elif options.network == 'vgg19':
	from keras_frcnn import vgg19 as nn
	C.network = 'vgg19'
elif options.network == 'mobilenetv1':
	from keras_frcnn import mobilenetv1 as nn
	C.network = 'mobilenetv1'
#	from keras.applications.mobilenet import preprocess_input
elif options.network == 'mobilenetv1_05':
	from keras_frcnn import mobilenetv1_05 as nn
	C.network = 'mobilenetv1_05'
#	from keras.applications.mobilenet import preprocess_input
elif options.network == 'mobilenetv1_25':
	from keras_frcnn import mobilenetv1_25 as nn
	C.network = 'mobilenetv1_25'
#	from keras.applications.mobilenet import preprocess_input
elif options.network == 'mobilenetv2':
	from keras_frcnn import mobilenetv2 as nn
	C.network = 'mobilenetv2'
else:
	print('Not a valid model')
	raise ValueError

# turn off any data augmentation at test time
C.use_horizontal_flips = False
C.use_vertical_flips = False
C.rot_90 = False

img_path = options.test_path

def format_img_size(img, C):
	""" formats the image size based on config """
	img_min_side = float(C.im_size)
	(height,width,_) = img.shape
		
	if width <= height:
		ratio = img_min_side/width
		new_height = int(ratio * height)
		new_width = int(img_min_side)
	else:
		ratio = img_min_side/height
		new_width = int(ratio * width)
		new_height = int(img_min_side)
	img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
	return img, ratio	

def format_img_channels(img, C):
	""" formats the image channels based on config """
	img = img[:, :, (2, 1, 0)]
	img = img.astype(np.float32)
	img[:, :, 0] -= C.img_channel_mean[0]
	img[:, :, 1] -= C.img_channel_mean[1]
	img[:, :, 2] -= C.img_channel_mean[2]
	img /= C.img_scaling_factor
	img = np.transpose(img, (2, 0, 1))
	img = np.expand_dims(img, axis=0)
	return img

def format_img(img, C):
	""" formats an image for model prediction based on config """
	img, ratio = format_img_size(img, C)
	img = format_img_channels(img, C)
	return img, ratio


# Method to transform the coordinates of the bounding box to its original size
def get_real_coordinates(ratio, x1, y1, x2, y2):

	real_x1 = int(round(x1 // ratio))
	real_y1 = int(round(y1 // ratio))
	real_x2 = int(round(x2 // ratio))
	real_y2 = int(round(y2 // ratio))

	return (real_x1, real_y1, real_x2 ,real_y2)

import pathlib

def replace_last(source_string, replace_what, replace_with):
    head, _sep, tail = source_string.rpartition(replace_what)
    return head + replace_with + tail

def derive_annotation_from_image_path(image_path: str):
	desired_path = pathlib.Path(image_path)
	name_only = str(desired_path.stem)
	parent_dir = str(desired_path.parent)

	annotation_dir = replace_last(parent_dir, "image", "annotation")
	annotation_path = pathlib.WindowsPath(annotation_dir).joinpath(f"{name_only}.xml")
	return str(annotation_path)

import xml.dom.minidom
def parse_int(s, base=10, val=None):
        if s.isdigit():
            return int(s, base)
        else:
            return val

def valid_coordinate(entry: str) -> bool:
	if entry is None or entry.strip() == '':
		return -1
	value = parse_int(entry.strip(),10,val=-1)
	if value == -1:
		return -1
	return value


def parse_annotation_file(annotation_file):
    doc = xml.dom.minidom.parse(annotation_file)

    object_node = doc.getElementsByTagName("object")
    
    if len(object_node) == 0 :
        #print(annotation_file, "empty")
        pass

    for obj in object_node:
        nameNode = obj.getElementsByTagName("name")
        bndBox = obj.getElementsByTagName("bndbox")

        for box in bndBox:
            xmin = obj.getElementsByTagName("xmin")[0].firstChild.data
            ymin = obj.getElementsByTagName("ymin")[0].firstChild.data
            xmax = obj.getElementsByTagName("xmax")[0].firstChild.data
            ymax = obj.getElementsByTagName("ymax")[0].firstChild.data

            xmin = valid_coordinate(xmin)
            ymin = valid_coordinate(ymin)
            xmax = valid_coordinate(xmax)
            ymax = valid_coordinate(ymax)

            has_boundingbox = xmin != -1 and ymin != -1 and xmax != -1 and ymax != -1 

            return (has_boundingbox, xmin, ymin, xmax, ymax)

class_mapping = C.class_mapping

if 'bg' not in class_mapping:
	class_mapping['bg'] = len(class_mapping)

class_mapping = {v: k for k, v in class_mapping.items()}
print(class_mapping)
class_to_color = {class_mapping[v]: np.random.randint(0, 255, 3) for v in class_mapping}
C.num_rois = int(options.num_rois)

if C.network == 'resnet50':
	num_features = 1024
elif C.network =="mobilenetv2":
	num_features = 320
else:
	# may need to fix this up with your backbone..!
	print("backbone is not resnet50. number of features chosen is 512")
	num_features = 512

if K.image_data_format() == 'channels_first':
	input_shape_img = (3, None, None)
	input_shape_features = (num_features, None, None)
else:
	input_shape_img = (None, None, 3)
	input_shape_features = (None, None, num_features)


img_input = Input(shape=input_shape_img)
roi_input = Input(shape=(C.num_rois, 4))
feature_map_input = Input(shape=input_shape_features)

# define the base network (resnet here, can be VGG, Inception, etc)
shared_layers = nn.nn_base(img_input)

# define the RPN, built on the base layers
num_anchors = len(C.anchor_box_scales) * len(C.anchor_box_ratios)
rpn_layers = nn.rpn(shared_layers, num_anchors)

classifier = nn.classifier(feature_map_input, roi_input, C.num_rois, nb_classes=len(class_mapping))

model_rpn = Model(img_input, rpn_layers)
model_classifier = Model([feature_map_input, roi_input], classifier)

# model loading
if options.load == None:
  print('Loading weights from {}'.format(C.model_path))
  model_rpn.load_weights(C.model_path, by_name=True)
  model_classifier.load_weights(C.model_path, by_name=True)
else:
  print('Loading weights from {}'.format(options.load))
  model_rpn.load_weights(options.load, by_name=True)
  model_classifier.load_weights(options.load, by_name=True)

#model_rpn.compile(optimizer='adam', loss='mse')
#model_classifier.compile(optimizer='adam', loss='mse')

all_imgs = []

classes = {}

bbox_threshold = 0.5

visualise = True

num_rois = C.num_rois

box_counter = 0

should_parse_annotation = True

for idx, img_name in enumerate(sorted(os.listdir(img_path))):
	if not img_name.lower().endswith(('.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff')):
		continue
	print(img_name)
	st = time.time()
	filepath = os.path.join(img_path,img_name)

	input_bbox = None
	if should_parse_annotation:
		annotation_file = derive_annotation_from_image_path(filepath)
		input_bbox = parse_annotation_file(annotation_file)

	img = cv2.imread(filepath)

    # preprocess image
	X, ratio = format_img(img, C)
	img_scaled = (np.transpose(X[0,:,:,:],(1,2,0)) + 127.5).astype('uint8')
	if K.image_data_format() == 'channels_last':
		X = np.transpose(X, (0, 2, 3, 1))
	# get the feature maps and output from the RPN
	[Y1, Y2, F] = model_rpn.predict(X)
	

	R = roi_helpers.rpn_to_roi(Y1, Y2, C, K.image_data_format(), overlap_thresh=0.3)
	#print(R.shape)
    
	# convert from (x1,y1,x2,y2) to (x,y,w,h)
	R[:, 2] -= R[:, 0]
	R[:, 3] -= R[:, 1]

	# apply the spatial pyramid pooling to the proposed regions
	bboxes = {}
	probs = {}
	for jk in range(R.shape[0]//num_rois + 1):
		ROIs = np.expand_dims(R[num_rois*jk:num_rois*(jk+1),:],axis=0)
		if ROIs.shape[1] == 0:
			break

		if jk == R.shape[0]//num_rois:
			#pad R
			curr_shape = ROIs.shape
			target_shape = (curr_shape[0],num_rois,curr_shape[2])
			ROIs_padded = np.zeros(target_shape).astype(ROIs.dtype)
			ROIs_padded[:,:curr_shape[1],:] = ROIs
			ROIs_padded[0,curr_shape[1]:,:] = ROIs[0,0,:]
			ROIs = ROIs_padded

		[P_cls,P_regr] = model_classifier.predict([F, ROIs])
		#print(P_cls)

		for ii in range(P_cls.shape[1]):

			if np.max(P_cls[0,ii,:]) < 0.8 or np.argmax(P_cls[0,ii,:]) == (P_cls.shape[2] - 1):
				continue

			cls_name = class_mapping[np.argmax(P_cls[0,ii,:])]

			if cls_name not in bboxes:
				bboxes[cls_name] = []
				probs[cls_name] = []
			(x,y,w,h) = ROIs[0,ii,:]

			bboxes[cls_name].append([16*x,16*y,16*(x+w),16*(y+h)])
			probs[cls_name].append(np.max(P_cls[0,ii,:]))

	all_dets = []

	found = False
	for key in bboxes:
		#print(key)
		#print(len(bboxes[key]))
		bbox = np.array(bboxes[key])

		new_boxes, new_probs = roi_helpers.non_max_suppression_fast(bbox, np.array(probs[key]), overlap_thresh = 0.3)
		for jk in range(new_boxes.shape[0]):
			found=True
			(x1, y1, x2, y2) = new_boxes[jk,:]
			(real_x1, real_y1, real_x2, real_y2) = get_real_coordinates(ratio, x1, y1, x2, y2)

			if input_bbox is not None and input_bbox[0] is True:
				_,xmin,ymin,xmax,ymax = input_bbox
				cv2.rectangle(img,(xmin, ymin), (xmax, ymax), (0,0,255),3)

			#cv2.rectangle(img,(real_x1, real_y1), (real_x2, real_y2), (int(class_to_color[key][0]), int(class_to_color[key][1]), int(class_to_color[key][2])),1)
			cv2.rectangle(img,(real_x1, real_y1), (real_x2, real_y2), (0,255,0),2)

			textLabel = '{}: {}'.format(key,int(100*new_probs[jk]))
			all_dets.append((key,100*new_probs[jk]))

			(retval,baseLine) = cv2.getTextSize(textLabel,cv2.FONT_HERSHEY_COMPLEX,1,1)
			textOrg = (real_x1, real_y1-0)

			cv2.rectangle(img, (textOrg[0] - 5, textOrg[1]+baseLine - 5), (textOrg[0]+retval[0] + 5, textOrg[1]-retval[1] - 5), (0, 0, 0), 2)
			cv2.rectangle(img, (textOrg[0] - 5,textOrg[1]+baseLine - 5), (textOrg[0]+retval[0] + 5, textOrg[1]-retval[1] - 5), (255, 255, 255), -1)
			cv2.putText(img, textLabel, textOrg, cv2.FONT_HERSHEY_DUPLEX, 1, (0, 0, 0), 1)

			box_counter += 1

	print('Elapsed time = {}'.format(time.time() - st),"counter: ", box_counter)
	#print(all_dets)
	#print(bboxes)
    # enable if you want to show pics
	if options.write and found:
           import os
           if not os.path.isdir("results"):
              os.mkdir("results")
           cv2.imwrite(f'./results/{img_name}_{idx}.png',img)
