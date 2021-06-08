"""
去雾代码主程序。
功能：
1. 结合DeBulrNet来做去雾处理
2. 训练时就测试
"""
from random import choice
import os
import glob
import numpy as np
import pickle
import tqdm
from dehazegan.losses import wasserstein_loss, perceptual_loss
from dehazegan.model import generator_model, discriminator_model, generator_containing_discriminator_multiple_outputs
from keras.optimizers import Adam
import matplotlib.pyplot as plt
import datetime
from skimage.measure import compare_ssim as ssim
from skimage.measure import compare_psnr
from PIL import Image

# 参数设置
batch_size = 1
epochs = 50

# 文件相关
# 模型保存目录
model_save_dir = './model_save_res_block_256_vgg16'
# TensorBoard log保存目录
log_dir = './logs'

# 图片数据相关
img_width = 256
img_height = 256


class DataLoader:
    """
    数据加载类
    """

    def __init__(self, batch_size) -> None:
        super().__init__()

        # 清晰图片目录
        self.clear_npys_dir = './datasets/npy/clear'
        # 雾图图片目录
        self.haze_npys_dir = './datasets/npy/haze'

        # 加载所有文件名
        self.all_clear_npys_names = self._load_paths(self.clear_npys_dir)
        self.all_haze_npys_names = self._load_paths(self.haze_npys_dir)

        self.file_nums = len(self.all_clear_npys_names)

        # 生成匹配对
        self.data_pairs = self._build_paris()

        # 训练数据，验证数据，测试数据占比
        self.train_percentage = 0.9
        self.validation_percentage = 0.05
        self.test_percentage = 1 - self.train_percentage - self.validation_percentage

        # 加载各数据生成器
        self.train_generator = self._build_data_generator(batch_size, 'train')
        self.val_generator = self._build_data_generator(batch_size, 'validation')
        self.test_generator = self._build_data_generator(batch_size, 'test')

    def _load_paths(self, dir):
        """
        加载数dir目录下的所有文件，并返回这些文件的路径集合(list)
        :param dir: 需要加载的目标目录
        :return: dir目录下的所有文件路径集合
        """
        return glob.glob(os.path.join(dir, "*.*"))

    def _build_paris(self):
        """
        由于一个清晰图对应多张雾图，为了训练时方便，这里形成训练数据对。具体是一个字典：
        1. "clear" -- 一张清晰图
        2. "haze" -- 多张雾图
        训练时可随机抽选一张雾图来对应一张clear。
        # TODO: 这个方案可能会改，后期可能会直接删除haze到只剩一张
        :return:一个列表list。list里面的每个元素都是一个字典。对应上面的'clear'和'haze'匹配对
        """
        # 持久数据对的路径
        pkl_name = './datasets/data_paris.pkl'

        if os.path.exists(pkl_name):
            pkl_file = open(pkl_name, 'rb')
            data_pairs = pickle.load(pkl_file)
            return data_pairs

        nums = len(self.all_clear_npys_names)
        # 预申请一些空间
        pairs = [{} for i in range(nums)]
        for idx, filepath in enumerate(self.all_clear_npys_names):
            filename = os.path.basename(filepath)
            # 去掉npy后缀
            filename = filename.replace(".npy", "")
            # 利用glob 去匹配所有haze图
            haze_file_paths = glob.glob(os.path.join(self.haze_npys_dir, filename + "*"))

            # 添加一个匹配对
            pairs[idx]['clear'] = filepath
            pairs[idx]['haze'] = haze_file_paths
        # 持久化
        output = open(pkl_name, 'wb')
        pickle.dump(pairs, output)
        return pairs

    def _build_data_generator(self, batch_size, mode='train'):
        """
        构造训练使用的生成器
        :param batch_size:
        :param mode: 需要哪类生成器 train | validation | test
        :return:
        """
        total_num = len(self.all_clear_npys_names)
        # 建立两个占位符
        # 0 - place1 - 1 为训练数据
        # place1 -- place2 -1 为验证数据
        # place2 - end 为测试数据
        place1 = int(total_num * self.train_percentage)
        place2 = int(total_num * (self.train_percentage + self.validation_percentage))

        # 确定数据范围
        if mode == 'train':
            start_idx, end_idx = 0, place1
        elif mode == 'validation':
            start_idx, end_idx = place1, place2
        elif mode == 'test':
            start_idx, end_idx = place2, total_num
        else:
            raise Exception("please use valid mode")

        pairs = self.data_pairs[start_idx:end_idx]
        pairs_num = len(pairs)

        x_datas = np.zeros((batch_size, img_height, img_width, 3))
        y_datas = np.zeros((batch_size, img_height, img_width, 3))

        def build_real_pairs(pairs):
            """
            paris中一个clear图对应多个haze图，本函数将从这多个haze图中，随机选取一个作为匹配对
            :param pairs:
            :return: x_datas,y_datas 元祖
            """
            num = len(pairs)
            x_datas = ['' for i in range(num)]
            y_datas = ['' for i in range(num)]

            for idx, pair in enumerate(pairs):
                x_datas[idx] = choice(pair['haze'])
                y_datas[idx] = pair['clear']
            return x_datas, y_datas

        pairs = np.array(pairs)

        # 产生生成器
        while True:
            # 随机扰乱
            permutated_indexes = np.random.permutation(pairs_num)

            if (pairs_num < batch_size):
                # 所有数据量不足以提供一个batch_size
                batch_paris = pairs[permutated_indexes]
                x_paths, y_paths = build_real_pairs(batch_paris)
                for idx in range(pairs_num):
                    x_datas[idx] = np.load(x_paths[idx])
                    y_datas[idx] = np.load(y_paths[idx])
                yield x_datas, y_datas
            else:
                # 数量能够提供
                for index in range(pairs_num // batch_size):
                    batch_indexes = permutated_indexes[index * batch_size:(index + 1) * batch_size]

                    # batch_pairs仅仅是些文件路径
                    batch_paris = pairs[batch_indexes]

                    x_paths, y_paths = build_real_pairs(batch_paris)
                    # 现在要把这些文件路径对应的npy文件读取成npy arr
                    for idx in range(batch_size):
                        x_datas[idx] = np.load(x_paths[idx])
                        y_datas[idx] = np.load(y_paths[idx])

                    yield x_datas, y_datas

    def load_seperate_test_datasets(self):
        """
        加载单独的训练数据集合。
        1. 训练数据集合放置与test_datasets/npy下
        :return:
        """
        file_paths = glob.glob(os.path.join('./test_datasets/npy', '*'))
        file_num = len(file_paths)

        haze_imgs = np.zeros((file_num, img_height, img_width, 3))
        for idx, file_path in enumerate(file_paths):
            haze_imgs[idx] = np.load(file_path)
        return haze_imgs


def save_all_weights(d, g, epoch_number, current_loss):
    g.save_weights(os.path.join(model_save_dir, 'generator_{}_{}.h5'.format(epoch_number, current_loss)), True)
    d.save_weights(os.path.join(model_save_dir, 'discriminator_{}.h5'.format(epoch_number)), True)


def load_saved_weight(g, d=None):
    """
    加载已训练好的权重
    :param g: 生成器
    :param d: 判别器
    :return:
    """
    # TODO: 这里需要做细化处理。判定文件是否存在。多个权重文件找到最新的权重文件
    # 这里为了方便，我是直接写死了最新训练的模型，自己可手动修改，也可以写个程序去找最新的权重模型
    g.load_weights(os.path.join(model_save_dir, 'generator_49_33.h5'))
    if d is None:
        return
    d.load_weights(os.path.join(model_save_dir, 'discriminator_49.h5'))

def test():
    """
    测试函数。计算指标
    :return:
    """
    # 构建网络模型
    g = generator_model('test')
    # 加载模型权重
    load_saved_weight(g)

    ##########################################
    # 测试集新代码。直接从jpg文件中读取，避免npy转
    #  case 1: 合成雾图去雾 生成去雾后的结果，并计算psnr，ssim
    #  case 2: 真实雾图去雾 生成去雾后的结果
    ##########################################
    def load_img_files(dir):
        """
        加载dir目录下的所有jpg后缀文件
        :param dir:
        :return: array数组
        """
        file_paths = glob.glob(os.path.join(dir, '*.jpg'))

        imgs = []
        for idx, file_path in enumerate(file_paths):
            imgs.append(np.array(Image.open(file_path).convert('RGB')))
        return np.array(imgs)
    def predict(g,haze_imgs):
        """
        输入haze_imgs，用g预测clear_imgs。
        之所以用这个函数，而不直接用g.predict，是为了适应haze_imgs中的img具有不同size的情况
        :param g
        :param haze_imgs: 雾图 size bound是 0 - 255
        :return: clear_imgs (每个clear_img可能具有不同的shape) size bound 是 0 -255
        """
        clear_imgs = []
        for haze_img in haze_imgs:
            haze_img = np.expand_dims(haze_img,axis=0)
            clear_img = g.predict(haze_img/127.5 - 1)[0]
            clear_imgs.append((clear_img + 1) * 127.5)
        return np.array(clear_imgs)

    mode = "real"  # synthesis or real
    # 清晰图目录
    clear_imgs_dir = ''
    # 雾图目录
    haze_imgs_dir = '../test_imgs'
    # 去雾结果保存目录
    dehaze_imgs_dir = '../test_imgs'
    if mode == "synthesis":
        clear_imgs = load_img_files(clear_imgs_dir)
        haze_imgs = load_img_files(haze_imgs_dir)

        # 去雾
        generated_imgs = predict(g,haze_imgs)

        # 初始化指标
        PSNR = 0
        SSIM = 0

        for idx, generated_img in enumerate(generated_imgs):
            dehazed_img = Image.fromarray(generated_img.astype('uint8'))
            dehazed_img.save(os.path.join(dehaze_imgs_dir, "%03d.jpg" % (idx + 1)))
            PSNR = PSNR + compare_psnr(clear_imgs[idx].astype('uint8'), generated_img.astype('uint8'))
            SSIM = SSIM + ssim(clear_imgs[idx].astype('uint8'), generated_img.astype('uint8'), multichannel=True)
        # 计算平均值
        PSNR = PSNR / len(generated_imgs)
        SSIM = SSIM / len(generated_imgs)
        print('PSNR',PSNR)
        print('SSIM',SSIM)
    elif mode == 'real':
        haze_imgs = load_img_files(haze_imgs_dir)
        # 去雾
        generated_imgs = predict(g,haze_imgs)

        for idx, generated_img in enumerate(generated_imgs):
            dehazed_img = Image.fromarray(generated_img.astype('uint8'))
            dehazed_img.save(os.path.join(dehaze_imgs_dir, "%03d.jpg" % (idx + 1)))

def train(batch_size, epochs, critic_updates=5):
    """
    训练网络
    :param batch_size:
    :param epochs:
    :param critic_updates: 每个batch_size 中 Discriminator需要训练的次数
    :return:
    """
    # 加载数据
    data_loader = DataLoader(batch_size)

    # 构建网络模型
    g = generator_model()
    # g.summary()
    d = discriminator_model()
    d.summary()
    d_on_g = generator_containing_discriminator_multiple_outputs(g, d)

    # 保存模型结构--用于可视化
    g.save(os.path.join(model_save_dir, "generator.h5"))
    d.save(os.path.join(model_save_dir, "discriminator.h5"))
    d_on_g.save(os.path.join(model_save_dir, "d_on_g.h5"))

    # 编译网络模型
    d_opt = Adam(lr=1E-4, beta_1=0.9, beta_2=0.999, epsilon=1e-08)
    d_on_g_opt = Adam(lr=1E-4, beta_1=0.9, beta_2=0.999, epsilon=1e-08)

    d.trainable = True
    d.compile(optimizer=d_opt, loss=wasserstein_loss)
    d.trainable = False
    loss = [perceptual_loss, wasserstein_loss]
    loss_weights = [100, 1]
    d_on_g.compile(optimizer=d_on_g_opt, loss=loss, loss_weights=loss_weights)
    d.trainable = True

    # 设置discriminator的real目标和fake目标
    output_true_batch, output_false_batch = np.ones((batch_size, 1)), -np.ones((batch_size, 1))
    # tensorboard_callback = TensorBoard(log_dir)

    # TODO: 可以在这里加入恢复权重，接力学习

    # 训练
    start = datetime.datetime.now()
    for epoch in tqdm.tqdm(range(epochs)):
        d_losses = []
        d_on_g_losses = []
        for index in range(data_loader.file_nums // batch_size):
            img_haze_batch, img_clear_batch = next(data_loader.train_generator)
            # 放缩到-1 - 1
            img_haze_batch = img_haze_batch / 127.5 - 1
            img_clear_batch = img_clear_batch / 127.5 - 1

            generated_images = g.predict(x=img_haze_batch, batch_size=batch_size)

            for _ in range(critic_updates):
                d_loss_real = d.train_on_batch(img_clear_batch, output_true_batch)
                d_loss_fake = d.train_on_batch(generated_images, output_false_batch)
                d_loss = 0.5 * np.add(d_loss_fake, d_loss_real)
                d_losses.append(d_loss)

            d.trainable = False

            d_on_g_loss = d_on_g.train_on_batch(img_haze_batch, [img_clear_batch, output_true_batch])
            d_on_g_losses.append(d_on_g_loss)

            d.trainable = True

            # print log
            print('d loss %f d_on_g loss %f' % (d_loss, d_on_g_loss[1] + d_on_g_loss[2]))

            if index % 50 == 0:
                # Test
                img_haze_test, img_clear_test = next(data_loader.test_generator)
                generated_images = g.predict(x=img_haze_test / 127.5 - 1, batch_size=batch_size)
                # 放缩为0-255
                generated_images = (generated_images + 1) * 127.5

                fig, axs = plt.subplots(batch_size, 3)
                for idx in range(batch_size):
                    axs[idx, 0].imshow((img_haze_test[idx].astype('uint8')))
                    axs[idx, 0].axis('off')
                    axs[idx, 0].set_title('haze')

                    axs[idx, 1].imshow((img_clear_test[idx].astype('uint8')))
                    axs[idx, 1].axis('off')
                    axs[idx, 1].set_title('origin')

                    axs[idx, 2].imshow(generated_images[idx].astype('uint8'))
                    axs[idx, 2].axis('off')
                    axs[idx, 2].set_title('dehazed')
                fig.savefig("./dehazed_result/image/dehazed/%d-%d.jpg" % (epoch, index))

        now = datetime.datetime.now()
        print(np.mean(d_losses), np.mean(d_on_g_losses), 'spend time %s' % (now - start))
        # 保存所有权重
        save_all_weights(d, g, epoch, int(np.mean(d_on_g_losses)))


if __name__ == '__main__':
    # train(2, 50, 4)
    test()