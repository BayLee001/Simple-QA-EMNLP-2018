from functools import lru_cache

import ctypes
import logging
import logging.config
import os
import time
import sys

import random
import torch
import numpy as np

from lib.text_encoders import PADDING_INDEX
from lib.datasets import Dataset

logger = logging.getLogger(__name__)


def resplit_datasets(dataset, other_dataset, random_seed=None, cut=None):
    """ Deterministic shuffle and split algorithm. 

    Given the same two datasets and the same `random_seed`, the split happens the same exact way
    every call.
    
    Args:
        dataset (lib.datasets.Dataset)
        other_dataset (lib.datasets.Dataset)
        random_seed (int, optional)
        cut (float, optional): float between 0 and 1 to cut the dataset; otherwise, the same
            proportions are kept.
    Returns:
        dataset (lib.datasets.Dataset)
        other_dataset (lib.datasets.Dataset)
    """
    concat = dataset.rows + other_dataset.rows
    # Reference:
    # https://stackoverflow.com/questions/19306976/python-shuffling-with-a-parameter-to-get-the-same-result
    # NOTE: Shuffle the same way every call of `shuffle_datasets` where the `random_seed` is given
    random.Random(random_seed).shuffle(concat)
    if cut is None:
        return Dataset(concat[:len(dataset)]), Dataset(concat[len(dataset):])
    else:
        cut = max(min(round(len(concat) * cut), len(concat)), 0)
        return Dataset(concat[:cut]), Dataset(concat[cut:])


def config_logging():
    """ Configure the root logger with basic settings.
    """
    logging.basicConfig(
        format='[%(asctime)s][%(processName)s][%(name)s][%(levelname)s] %(message)s',
        level=logging.INFO,
        stream=sys.stdout)


def get_root_path():
    """ Get the path to the root directory
    
    Returns (str):
        Root directory path
    """
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), '..')


def new_experiment_folder(label='', parent_directory='experiments/'):
    """
    Get a experiment directory that includes start time.
    """
    start_time = time.time()
    name = '%s.%s' % (label, time.strftime('%m_%d_%H:%M:%S', time.localtime()))
    path = os.path.join(parent_directory, name)
    os.makedirs(path)

    # TODO: If the folder is empty then delete it after the execution finishes

    return path


# Reference:
# https://stackoverflow.com/questions/8290397/how-to-split-an-iterable-in-constant-size-chunks
def batch(iterable, n=1):
    if not hasattr(iterable, '__len__'):
        # Slow version if len is not defined
        current_batch = []
        for item in iterable:
            current_batch.append(item)
            if len(current_batch) == n:
                yield current_batch
                current_batch = []
        if current_batch:
            yield current_batch
    else:
        # Fast version is len is defined
        l = len(iterable)
        for ndx in range(0, l, n):
            yield iterable[ndx:min(ndx + n, l)]



# Reference: https://stackoverflow.com/questions/4675728/redirect-stdout-to-a-file-in-python
class StreamFork(object):

    def __init__(self, filename, stream):
        self.stream = stream
        self.file_ = open(filename, 'a')

    @property
    def closed(self):
        return self.file_.closed and self.stream.closed

    def write(self, message):
        self.stream.write(message)
        self.file_.write(message)

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def flush(self):
        self.file_.flush()
        self.stream.flush()

    def close(self):
        self.file_.close()
        self.stream.close()


def save_standard_streams(directory=''):
    """
    Save stdout and stderr to a `{directory}/stdout.log` and `{directory}/stderr.log`.
    """
    sys.stdout = StreamFork(os.path.join(directory, 'stdout.log'), sys.stdout)
    sys.stderr = StreamFork(os.path.join(directory, 'stderr.log'), sys.stderr)


def device_default(device=None):
    """
    Using torch, return the default device to use.
    Args:
        device (int or None): -1 for CPU, None for default GPU or CPU, and 0+ for GPU device ID
    Returns:
        device (int or None): -1 for CPU and 0+ for GPU device ID
    """
    if device is None:
        device = torch.cuda.current_device() if torch.cuda.is_available() else -1
    return device


@lru_cache(maxsize=1)
def cuda_devices():
    """
    Checks for all CUDA devices with free memory.
    Returns:
        (list [int]) the CUDA devices available
    """

    # Find Cuda
    cuda = None
    for libname in ('libcuda.so', 'libcuda.dylib', 'cuda.dll'):
        try:
            cuda = ctypes.CDLL(libname)
        except OSError:
            continue
        else:
            break

    # Constants taken from cuda.h
    CUDA_SUCCESS = 0

    num_gpu = ctypes.c_int()
    error = ctypes.c_char_p()
    free_memory = ctypes.c_size_t()
    total_memory = ctypes.c_size_t()
    context = ctypes.c_void_p()
    device = ctypes.c_int()
    ret = []  # Device IDs that are not used.

    def run(result, func, *args):
        result = func(*args)
        if result != CUDA_SUCCESS:
            cuda.cuGetErrorString(result, ctypes.byref(error))
            logger.warn("%s failed with error code %d: %s", func.__name__, result,
                        error.value.decode())
            return False
        return True

    # Check if Cuda is available
    if not cuda:
        return ret

    result = cuda.cuInit(0)

    # Get number of GPU
    if not run(result, cuda.cuDeviceGetCount, ctypes.byref(num_gpu)):
        return ret

    for i in range(num_gpu.value):
        if (not run(result, cuda.cuDeviceGet, ctypes.byref(device), i) or
                not run(result, cuda.cuDeviceGet, ctypes.byref(device), i) or
                not run(result, cuda.cuCtxCreate, ctypes.byref(context), 0, device) or
                not run(result, cuda.cuMemGetInfo,
                        ctypes.byref(free_memory), ctypes.byref(total_memory))):
            continue

        percent_free_memory = float(free_memory.value) / total_memory.value
        logger.info('CUDA device %d has %f free memory [%d MiB of %d MiB]', i, percent_free_memory,
                    free_memory.value / 1024**2, total_memory.value / 1024**2)
        if percent_free_memory > 0.98:
            logger.info('CUDA device %d is available', i)
            ret.append(i)

        cuda.cuCtxDetach(context)

    return ret


def get_total_parameters(model):
    """ Return the total number of trainable parameters in model """
    params = filter(lambda p: p.requires_grad, model.parameters())
    return sum(x.size()[0] * x.size()[1] if len(x.size()) > 1 else x.size()[0] for x in params)


def pad_tensor(tensor, length):
    """ Pad a tensor to length with PADDING_INDEX.
    
    Args:
        tensor (torch.LongTensor)
    Returns
        torch.LongTensor
    """
    n_padding = length - len(tensor)
    padding = torch.LongTensor(n_padding * [PADDING_INDEX])
    return torch.cat((tensor, padding), 0)


def pad_batch(batch):
    """ Pad a list of tensors with PADDING_INDEX.
    
    Args:
        batch (list of torch.LongTensor)
    Returns
        list of torch.LongTensor and original lengths
    """
    # PyTorch RNN requires batches to be sorted in decreasing length order
    lengths = [len(row) for row in batch]
    max_len = max(lengths)
    padded = [pad_tensor(row, max_len) for row in batch]
    return padded, lengths


def seed(random_seed, is_cuda=False):
    """
    Attempt to apply a `random_seed` is every possible library that may require it. Our goal is 
    to make our software reporducible.
    """
    random.seed(random_seed)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    if is_cuda:
        torch.cuda.manual_seed(random_seed)
        torch.cuda.manual_seed_all(random_seed)
    torch.backends.cudnn.deterministic = True
    logger.info('Seed: %s', random_seed)


def torch_equals_ignore_index(tensor, tensor_other, ignore_index=None):
    """
    Compute torch.equals with the optional mask parameter.
   
    Args:
        ignore_index (int, optional): specifies a tensor1 index that is ignored
    Returns:
        (bool) iff target and prediction are equal
    """
    if ignore_index is not None:
        assert tensor.size() == tensor_other.size()
        mask_arr = tensor.ne(ignore_index)
        tensor = tensor.masked_select(mask_arr)
        tensor_other = tensor_other.masked_select(mask_arr)

    return torch.equal(tensor, tensor_other)
