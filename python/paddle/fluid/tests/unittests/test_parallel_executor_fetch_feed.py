# Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import print_function

import math
import paddle.fluid as fluid
from paddle.fluid import compiler
import paddle.fluid.core as core
import unittest
import numpy as np
import os


def Lenet(data, class_dim):
    conv1 = fluid.layers.conv2d(data, 32, 5, 1, act=None)
    bn1 = fluid.layers.batch_norm(conv1, act='relu')
    pool1 = fluid.layers.pool2d(bn1, 2, 'max', 2)
    conv2 = fluid.layers.conv2d(pool1, 50, 5, 1, act=None)
    bn2 = fluid.layers.batch_norm(conv2, act='relu')
    pool2 = fluid.layers.pool2d(bn2, 2, 'max', 2)

    fc1 = fluid.layers.fc(pool2, size=500, act='relu')
    fc2 = fluid.layers.fc(fc1, size=class_dim, act='softmax')

    return fc2


class TestFetchAndFeed(unittest.TestCase):
    def parallel_exe(self, use_cuda, run_parallel_exe, seed=1):
        main_program = fluid.Program()
        startup = fluid.Program()
        startup.random_seed = seed
        with fluid.program_guard(main_program, startup):
            data = fluid.layers.data(
                name='image', shape=[3, 224, 224], dtype='float32')
            label = fluid.layers.data(name='label', shape=[1], dtype='int64')
            out = Lenet(data, class_dim=102)
            loss = fluid.layers.cross_entropy(input=out, label=label)
            loss = fluid.layers.mean(loss)
            opt = fluid.optimizer.Momentum(
                learning_rate=0.1,
                momentum=0.9,
                regularization=fluid.regularizer.L2Decay(1e-4))
            opt.minimize(loss)

        place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
        exe = fluid.Executor(place)
        exe.run(startup)

        pe = fluid.ParallelExecutor(
            use_cuda=use_cuda, loss_name=loss.name, main_program=main_program)
        run_parallel_exe(main_program, pe, use_cuda, data, label, loss)

    def run_parallel_exe_with_fetch(self, main, pe, use_cuda, data, label,
                                    loss):
        def get_data(batch_size=8):
            np.random.seed(5)
            while True:
                img = np.random.random(
                    size=[batch_size, 3, 224, 224]).astype(np.float32)
                l = (np.random.random(size=[batch_size, 1]) *
                     10).astype(np.int64)
                yield img, l

        # TODO(zcd): I found that onece the memory optimizer is open,
        # parallel_exe doesn't fetch some variable, such as conv2d_0.b_0@GRAD,
        # conv2d_1.b_0@GRAD. Those variables should not be pruned.
        # fluid.memory_optimize(main)
        fetch_list = []
        all_vars = main.global_block().vars

        for k, v in all_vars.items():
            if ('tmp' not in k) and (
                    k[0] is not '_' or v.persistable
            ) and v.type == core.VarDesc.VarType.LOD_TENSOR:
                fetch_list.append(k)

        for batch_id, img_label in enumerate(get_data()):
            img, l = img_label
            train_inputs = {data.name: img, label.name: l}
            ret = pe.run(fetch_list, feed=train_inputs, return_numpy=True)
            for i in range(len(fetch_list)):
                assert not math.isnan(np.sum(ret[i])) and \
                       not math.isinf(np.sum(ret[i]))
            if batch_id == 2:
                break

    def run_parallel_exe_with_feed(self, main, pe, use_cuda, data, label, loss):
        def get_data(batch_size=8):
            np.random.seed(5)
            while True:
                train_data = []
                for _ in range(batch_size):
                    img = np.random.random(
                        size=[1, 3, 224, 224]).astype(np.float32)
                    label = (np.random.random(size=[1, 1]) *
                             10).astype(np.int64)
                    train_data.append([img, label])
                yield train_data

        place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
        feeder = fluid.DataFeeder(place=place, feed_list=[data, label])
        reader = feeder.decorate_reader(get_data, multi_devices=True)

        for batch_id, data in enumerate(reader()):
            loss_np = exe.run(train_cp, feed=data, fetch_list=[loss.name])[0]
            print(batch_id, loss_np)
            if batch_id == 2:
                break

    def test_fetch(self):
        os.environ['CPU_NUM'] = str(4)
        if core.is_compiled_with_cuda():
            self.parallel_exe(
                use_cuda=True,
                run_parallel_exe=self.run_parallel_exe_with_fetch)
        self.parallel_exe(
            use_cuda=False, run_parallel_exe=self.run_parallel_exe_with_fetch)

    def test_feed(self):
        os.environ['CPU_NUM'] = str(4)
        if core.is_compiled_with_cuda():
            self.parallel_exe(
                use_cuda=True, run_parallel_exe=self.run_parallel_exe_with_feed)
        self.parallel_exe(
            use_cuda=False, run_parallel_exe=self.run_parallel_exe_with_feed)


if __name__ == '__main__':
    unittest.main()
