"""
Keras implementation of CapsNet in Hinton's paper Dynamic Routing Between Capsules.
Author: Xifeng Guo, E-mail: `guoxifeng1990@163.com`, Github: `https://github.com/XifengGuo/CapsNet-Keras`

Modified by Maxim Peterson, max@singularitynet.io
"""

import numpy as np
import tensorflow as tf
from keras import layers, models, optimizers
from keras import backend as K
from keras import callbacks
import utils
from capsulelayers import CapsuleLayer, PrimaryCap, Length, Mask
from test_model import test_model
import os, argparse
from PIL import Image
K.set_image_data_format('channels_last')


from tensorflow.python.client import device_lib
print(device_lib.list_local_devices())

# os.environ["CUDA_VISIBLE_DEVICES"] = "0"

sx = 28
sy = 28
BATCH_SIZE = 32
NCLASSES = 8

# Angles for 3 and 4 rotation
ang_min = -45
ang_max = 46

#ang_min = 46
#ang_max = 315

#ang_min = 180-45
#ang_max = 180+46

#is_only_3_and_4 = True
is_only_3_and_4 = False

def CapsNet(input_shape, n_class, routings):
    """
    A Capsule Network on MNIST.
    :param input_shape: data shape, 3d, [width, height, channels]
    :param n_class: number of classes
    :param routings: number of routing iterations
    :return: Two Keras Models, the first one used for training, and the second one for evaluation.
            `eval_model` can also be used for training.
    """
    x = layers.Input(shape=input_shape)

    # Layer 1: Just a conventional Conv2D layer
    conv1 = layers.Conv2D(filters=256, kernel_size=9, strides=1, padding='valid', activation='relu', name='conv1')(x)

    # Layer 2: Conv2D layer with `squash` activation, then reshape to [None, num_capsule, dim_capsule]
    primarycaps = PrimaryCap(conv1, dim_capsule=8, n_channels=32, kernel_size=9, strides=2, padding='valid')

    # Layer 3: Capsule layer. Routing algorithm works here.
    digitcaps = CapsuleLayer(num_capsule=n_class, dim_capsule=16, routings=routings,
                             name='digitcaps')(primarycaps)

    # Layer 4: This is an auxiliary layer to replace each capsule with its length. Just to match the true label's shape.
    # If using tensorflow, this will not be necessary. :)
    out_caps = Length(name='capsnet')(digitcaps)

    # Decoder network.
    y = layers.Input(shape=(n_class,))
    masked_by_y = Mask()([digitcaps, y])  # The true label is used to mask the output of capsule layer. For training
    masked = Mask()(digitcaps)  # Mask using the capsule with maximal length. For prediction

    # Shared Decoder model in training and prediction
    decoder = models.Sequential(name='decoder')
    decoder.add(layers.Dense(512, activation='relu', input_dim=16*n_class))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(np.prod(input_shape), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=input_shape, name='out_recon'))


    # Models for training and evaluation (prediction)
    train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
    eval_model = models.Model(x, [out_caps, decoder(masked)])

    return train_model, eval_model


def margin_loss(y_true, y_pred):
    """
    Margin loss for Eq.(4). When y_true[i, :] contains not just one `1`, this loss should work too. Not test it.
    :param y_true: [None, n_classes]
    :param y_pred: [None, num_capsule]
    :return: a scalar loss value.
    """
    L = y_true * K.square(K.maximum(0., 0.9 - y_pred)) + \
        0.5 * (1 - y_true) * K.square(K.maximum(0., y_pred - 0.1))

    return K.mean(K.sum(L, 1))

def train(model, data, args):
    """
    Training a CapsuleNet
    :param model: the CapsuleNet model
    :param data: a tuple containing training and testing data, like `((x_train, y_train), (x_test, y_test))`
    :param args: arguments
    :return: The trained model
    """
    # unpacking the data
    (x_train, y_train), (x_test, y_test) = data

    # callbacks
    log = callbacks.CSVLogger(args.save_dir + '/log.csv')
    tb = callbacks.TensorBoard(log_dir=args.save_dir + '/tensorboard-logs',
                               batch_size=args.batch_size, histogram_freq=int(args.debug))
    checkpoint = callbacks.ModelCheckpoint(args.save_dir + '/weights-{epoch:02d}.h5', monitor='val_capsnet_acc',
                                           save_best_only=True, save_weights_only=True, verbose=1)
    lr_decay = callbacks.LearningRateScheduler(schedule=lambda epoch: args.lr * (args.lr_decay ** epoch))

    # compile the model
    model.compile(optimizer=optimizers.Adam(lr=args.lr),
                  loss=[margin_loss, 'mse'],
                  loss_weights=[1., args.lam_recon],
                  metrics={'capsnet': 'accuracy'})


    # Begin: Training with rotated data ---------------------------------------------------------------------#

    def mnist_rot_excl_train_generator(x, y, batch_size):
        nImg = x.shape[0]
        k = 0
        while 1:
            numList = [np.random.randint(0, nImg) for r in range(batch_size)]
            # x_batch = np.zeros([batch_size, 28, 28, 1], dtype=np.float32)
            # y_batch = np.zeros(batch_size, dtype=np.int32)

            x_batch = x[numList, :, :, :]
            y_batch = y[numList, :]

            for i in range(batch_size):
                x_batch[i, :, :, :] = utils.create_inputs_mnist_rot_excl(x_batch[i, :, :, :], y_batch[i, :])

            # if ((k+1)%1000):
            #     img = utils.combine_images(x_batch)
            #     image = img * 255
            #     img_name = args.save_dir + "/step_{}.png".format(str(k))
            #     Image.fromarray(image.astype(np.uint8)).save(img_name)
            # k += 1

            yield ([x_batch, y_batch], [y_batch, x_batch])



    model.fit_generator(generator=mnist_rot_excl_train_generator(x_train, y_train, args.batch_size),
                        steps_per_epoch=int(y_train.shape[0] / args.batch_size),
                        epochs=args.epochs,
                        validation_data=[[x_test, y_test], [y_test, x_test]],
                        callbacks=[log, tb, checkpoint, lr_decay])

    # End: Training with rotated data-----------------------------------------------------------------------#

    model.save_weights(args.save_dir + '/trained_model.h5')
    print('Trained model saved to \'%s/trained_model.h5\'' % args.save_dir)


    return model



if __name__ == "__main__":

    # setting the hyper parameters
    parser = argparse.ArgumentParser(description="Capsule Network on MNIST.")
    parser.add_argument('--epochs', default=1, type=int)
    parser.add_argument('--batch_size', default=100, type=int)
    parser.add_argument('--lr', default=0.001, type=float,
                        help="Initial learning rate")
    parser.add_argument('--lr_decay', default=0.9, type=float,
                        help="The value multiplied by lr at each epoch. Set a larger value for larger epochs")
    parser.add_argument('--lam_recon', default=0.392, type=float,
                        help="The coefficient for the loss of decoder")
    parser.add_argument('-r', '--routings', default=3, type=int,
                        help="Number of iterations used in routing algorithm. should > 0")
    parser.add_argument('--shift_fraction', default=0.1, type=float,
                        help="Fraction of pixels to shift at most in each direction.")
    parser.add_argument('--debug', action='store_true',
                        help="Save weights by TensorBoard")
    parser.add_argument('--save_dir', default='./result_mnist_rot_excl')
    parser.add_argument('-t', '--testing', action='store_true',
                        help="Test the trained model on testing dataset")
    parser.add_argument('--digit', default=5, type=int,
                        help="Digit to manipulate")
    parser.add_argument('-w', '--weights', default=None,
                        help="The path of the saved weights. Should be specified when testing")
    # parser.add_argument('-w', '--weights', default='./trained_models/trained_model.h5',
    #                      help="The path of the saved weights. Should be specified when testing")
    parser.add_argument('--n_test', default=3, type=int, help="Number of tests to run")

    args = parser.parse_args()
    print(args)

    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    # load data
    (x_train, y_train), (x_test_all, y_test_all) = utils.load_mnist_excluded()

    model, eval_model = CapsNet(input_shape=x_train.shape[1:],
                    n_class=len(np.unique(np.argmax(y_train, 1))),
                    routings=args.routings)

    model.summary()

    # train or test
    if args.weights is not None:  # init the model weights with provided one
        model.load_weights(args.weights)
    if not args.testing:
        train(model=model, data=((x_train, y_train), (x_test_all, y_test_all)), args=args)
    else:  # as long as weights are given, will run testing
        if args.weights is None:
            print('No weights are provided. Will test using random initialized weights.')

        if not is_only_3_and_4:
            test_model(eval_model, args.n_test, x_test_all, y_test_all,
                            BATCH_SIZE, ang_min, ang_max)
        else:
            nImg = x_test_all.shape[0]
            x_test = np.empty([1, sy, sx, 1], dtype=np.float32)
            y_test = np.empty([1, NCLASSES])
            k = 0
            for i in range(nImg):
                y_i = y_test_all[i, :]
                y_i = np.argmax(y_i)
                if (y_i == 3) or (y_i == 4):
                    if k == 0:
                        x_test[0, :, :, :] = x_test_all[i, :, :, :]
                        y_test[0, :] = y_test_all[i, :]
                    else:
                        x_test = np.concatenate([x_test, np.expand_dims(x_test_all[i, :, :, :], 0)])
                        y_test = np.concatenate([y_test, np.expand_dims(y_test_all[i, :], 0)])

                    k += 1

            test_model(eval_model, args.n_test, x_test, y_test, BATCH_SIZE, ang_min, ang_max)
