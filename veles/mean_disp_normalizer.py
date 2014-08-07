"""
Created on Jul 4, 2014

Normalizes multichannel byte images according to
dataset mean and dispersion.

Copyright (c) 2014 Samsung Electronics Co., Ltd.
"""


import numpy
from zope.interface import implementer

from veles.config import root
import veles.error as error
from veles.formats import Vector, assert_addr
from veles.distributable import IDistributable, TriviallyDistributable
from veles.opencl_types import numpy_dtype_to_opencl
from veles.opencl_units import OpenCLUnit, IOpenCLUnit


@implementer(IOpenCLUnit, IDistributable)
class MeanDispNormalizer(OpenCLUnit, TriviallyDistributable):
    """Normalizes multichannel byte images according to
    dataset mean and dispersion.

    Attributes:
        input: minibatch of images (dtype=numpy.uint8,
                                    shape[0]=minibatch_size).
        mean: mean image over the dataset (dtype=numpy.uint8).
        rdisp: 1.0 / dispersion over the dataset (float datatype).
        output: normalized float images of the same dtype as rdisp.
    """
    def __init__(self, workflow, **kwargs):
        kwargs["view_group"] = kwargs.get("view_group", "WORKER")
        super(MeanDispNormalizer, self).__init__(workflow, **kwargs)
        self.input = None
        self.mean = None
        self.rdisp = None
        self.output = Vector()
        self.demand("input", "mean", "rdisp")
        self.global_size = None
        self.local_size = None

    def init_unpickled(self):
        super(MeanDispNormalizer, self).init_unpickled()
        self.cl_sources_["mean_disp_normalizer.cl"] = {}

    def initialize(self, device, **kwargs):
        super(MeanDispNormalizer, self).initialize(device, **kwargs)

        if not isinstance(self.input, Vector) or self.input.mem is None:
            raise error.BadFormatError("input should be assigned as Vector")
        if not isinstance(self.mean, Vector) or self.mean.mem is None:
            raise error.BadFormatError("mean should be assigned as Vector")
        if not isinstance(self.rdisp, Vector) or self.rdisp.mem is None:
            raise error.BadFormatError("rdisp should be assigned as Vector")
        if len(self.input.shape) < 2:
            raise error.BadFormatError("input should be at least 2D")
        sample_size = self.mean.size
        if (self.input.sample_size != sample_size or
                self.rdisp.size != sample_size):
            raise error.BadFormatError("Sample size of input differs from "
                                       "mean-rdisp size")

        dtype = self.rdisp.dtype

        if self.output.mem is None or self.output.size != self.input.size:
            self.output.reset()
            sh = self.input.shape
            if root.common.unit_test:  # for overflow test
                sh = list(sh)
                sh[0] <<= 1
                self.output.mem = numpy.zeros(sh, dtype=dtype)
                self.output.initialize(device)
                self.output.map_write()
                self.output.vv = self.output.mem
                sh[0] >>= 1
                self.output.mem = self.output.vv[:sh[0]]
                assert_addr(self.output.mem, self.output.vv)
                self.output.vv[sh[0]:] = numpy.nan
            else:
                self.output.mem = numpy.zeros(sh, dtype=dtype)

        self.input.initialize(device)
        self.mean.initialize(device)
        self.rdisp.initialize(device)
        self.output.initialize(device)

        if self.device is None:
            return

        defines = {
            "input_type": numpy_dtype_to_opencl(self.input.dtype),
            "mean_type": numpy_dtype_to_opencl(self.mean.dtype),
            "SAMPLE_SIZE": sample_size
        }
        self.build_program(
            defines, "%s/mean_disp_normalizer.cl" % root.common.cache_dir,
            dtype=dtype)

        self.assign_kernel("normalize_mean_disp")
        self.set_args(self.input, self.mean, self.rdisp, self.output)

        self.global_size = [sample_size, self.input.shape[0]]

    def ocl_run(self):
        self.input.unmap()
        self.mean.unmap()
        self.rdisp.unmap()
        self.output.unmap()
        self.execute_kernel(self.global_size, self.local_size)

    def cpu_run(self):
        self.input.map_read()
        self.mean.map_read()
        self.rdisp.map_read()
        self.output.map_invalidate()

        dtype = self.output.dtype
        self.output.matrix[:] = (
            self.input.matrix.astype(dtype)[:] -
            self.mean.plain.astype(dtype)) * self.rdisp.plain
