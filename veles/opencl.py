"""
Created on Mar 21, 2013

OpenCL helper classes.

Copyright (c) 2013 Samsung Electronics Co., Ltd.
"""


import argparse
import numpy
import os
import six
from six.moves import cPickle as pickle
import sys
import time
import traceback
import opencl4py as cl

from veles.config import root, get
import veles.formats as formats
import veles.opencl_types as opencl_types
import veles.rnd as rnd
import veles.units as units
import veles.external.prettytable as prettytable


PYVER = 3 if six.PY3 else 2


class DeviceInfo(object):
    """Info about device.

    Attributes:
        desc: Description of the device.
        memsize: "available" size of the memory on the device.
        memalign: best alignment for device buffers.
        version: OpenCL version.
        rating: in [0, 1] interval (1 - fastest, 0.5 - 50% slower than fastest,
                0 - unrated).
        dt: time of rating test pass.
        min_dt: minimum time of rating test pass of all tests.
        BLOCK_SIZE: best block size for matrix multiplication for the device.
    """
    def __init__(self, desc, memsize, memalign, version):
        self.desc = desc
        self.memsize = memsize
        self.memalign = memalign
        self.version = version
        self.rating = {}
        for dtype in opencl_types.dtypes.keys():
            self.rating[dtype] = 0.0
        self.dt = {}
        for dtype in opencl_types.dtypes.keys():
            self.dt[dtype] = 86400
        self.min_dt = {}
        for dtype in opencl_types.dtypes.keys():
            self.min_dt[dtype] = 86400
        self.BLOCK_SIZE = {}
        for dtype in opencl_types.dtypes.keys():
            self.BLOCK_SIZE[dtype] = 8


class Device(units.Pickleable):
    """OpenCL device helper class.

    Attributes:
        device_info: DeviceInfo object.
        context_: OpenCL context handle.
        queue_: OpenCL device queue.
        pid_: process id.
    """
    def __init__(self):
        super(Device, self).__init__()
        self._get_some_device()
        self._fill_device_info_performance_values()
        log_configs = "Selected the following OpenCL configurations:\n"
        table = prettytable.PrettyTable("device", " dtype", "rating",
                                        "BLOCK_SIZE", "memalign", "version")
        table.align["device"] = "l"
        table.align[" dtype"] = "l"
        for dtype in sorted(opencl_types.dtypes.keys()):
            table.add_row(self.device_info.desc, dtype,
                          "%.2f" % self.device_info.rating[dtype],
                          self.device_info.BLOCK_SIZE[dtype],
                          self.device_info.memalign,
                          self.device_info.version)
        self.info(log_configs + str(table))

    def init_unpickled(self):
        super(Device, self).init_unpickled()
        self.queue_ = None
        self.pid_ = os.getpid()

    @staticmethod
    def arg_completer(prefix, **kwargs):
        def format_device(platform, device):
            return "%s - %s on %s" % (device.path, device.name.strip(),
                                      platform.name)

        if prefix.strip() == "":
            platforms = cl.Platforms().platforms
            if len(platforms) == 1 and len(platforms[0].devices) == 1:
                return ["0:0"]
            result = []
            for platform in platforms:
                for device in platform:
                    result.append(format_device(platform, device))
            return result
        parsed = [p for p in prefix.split(':') if p.strip() != ""]
        platform = cl.Platforms().platforms[int(parsed[0].strip())]
        if len(parsed) == 1:
            if len(platform.devices) == 1:
                return [platform.devices[0].path]
            result = []
            for device in platform:
                result.append(format_device(platform, device))
            return result

    @staticmethod
    def init_parser(**kwargs):
        parser = kwargs.get("parser", argparse.ArgumentParser())
        parser.add_argument(
            "-d", "--device", type=str, default="",
            help="OpenCL device to use.").completer = Device.arg_completer
        return parser

    def _get_some_device(self, **kwargs):
        """Gets some device from the available OpenCL devices.
        """
        parser = Device.init_parser(**kwargs)
        args, _ = parser.parse_known_args()
        platforms = cl.Platforms()
        if args.device == "":
            context = platforms.create_some_context()
        else:
            platfnum, devnums = args.device.split(':')
            platform = platforms.platforms[int(platfnum)]
            context = platform.create_context(
                [platform.devices[int(devnum)]
                 for devnum in devnums.split(',')])
        device = context.devices[0]
        desc = "%s/%s/%d" % (device.vendor.strip(), device.name.strip(),
                             device.vendor_id)
        self.device_info = DeviceInfo(
            desc=desc, memsize=device.memsize,
            memalign=device.memalign, version=device.version)
        self.queue_ = context.create_queue(
            device, cl.CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE)

    def _fill_device_info_performance_values(self):
        device_infos = {}
        try:
            fin = open(os.path.join(root.common.device_dir,
                                    "device_infos.%d.pickle" % PYVER),
                       "rb")

            # TODO(lyubov.p): recreate device_infos.*.pickle,
            # then remove add_path.

            def add_path(path):
                if path not in sys.path:
                    sys.path.append(path)

            add_path(root.common.opencl_dir)

            device_infos = pickle.load(fin)

            fin.close()
        except IOError:
            self.warning(os.path.join(root.common.device_dir,
                                      "device_infos.%d.pickle" % PYVER) +
                         " was not found")
        if (not root.common.test_known_device and
           self.device_info.desc in device_infos):
            device_info = device_infos[self.device_info.desc]
            self.device_info.rating.update(device_info.rating)
            self.device_info.BLOCK_SIZE.update(device_info.BLOCK_SIZE)
            self.device_info.dt.update(device_info.dt)
            self.device_info.min_dt.update(device_info.min_dt)
            return
        if not root.common.test_unknown_device:
            return
        device_infos[self.device_info.desc] = self.device_info
        self._do_tests(device_infos)
        fout = open(os.path.join(root.common.device_dir,
                                 "device_infos.%d.pickle" % PYVER),
                    "wb")
        pickle.dump(device_infos, fout)
        fout.close()
        self.info("Saved the measured device performance values to %s" %
                  (os.path.join(root.common.device_dir,
                                "device_infos.%d.pickle" % PYVER)))

    def _do_tests(self, device_infos):
        """Measure relative device performance.
        """
        bs_max = get(root.common.opencl.benchmark.max_block_size, 32)
        bs_min = get(root.common.opencl.benchmark.min_block_size, 3)
        if bs_min >= bs_max:
            raise ValueError("max_block_size must be greater than "
                             "min_block_size")
        self.info(
            "Testing device performance on block sizes (%d, %d].\n"
            "Results will be saved to " % (bs_min, bs_max) +
            os.path.join(root.common.device_dir,
                         "device_infos.%d.pickle, " % PYVER) +
            "so this is usually a one time process.")

        min_dt = {}
        dt_numpy = {}
        for dtype in opencl_types.dtypes.keys():
            min_dt[dtype] = 86400
            dt_numpy[dtype] = 86400
        for device_info in device_infos.values():
            for dtype in device_info.min_dt.keys():
                min_dt[dtype] = device_info.min_dt[dtype]
            break

        cc = {}
        for dtype in self.device_info.dt.keys():
            self.device_info.dt[dtype] = 86400
        for BLOCK_SIZE in range(bs_max, bs_min, -1):
            for dtype in sorted(opencl_types.dtypes.keys()):
                try:
                    self._prepare_test(BLOCK_SIZE, dtype, cc)
                    key = ("%s_%d_%d_%d"
                           % (dtype, self.AB_WIDTH,
                              self.B_HEIGHT, self.A_HEIGHT))
                    if not key in cc.keys():
                        self.info("Numpy for dtype=%s" % (dtype))
                        dt = self._do_cpu_test(cc, key)
                        self.info("Done in %.3f seconds" % (dt))
                        if dt < dt_numpy[dtype]:
                            dt_numpy[dtype] = dt
                        if dt_numpy[dtype] < min_dt[dtype]:
                            min_dt[dtype] = dt_numpy[dtype]
                    self.info(
                        "Testing %s with BLOCK_SIZE = %d "
                        "and dtype = %s" % (self.device_info.desc, BLOCK_SIZE,
                                            dtype))
                    dt = self._do_test(BLOCK_SIZE, dtype, 3)
                    if dt < self.device_info.dt[dtype]:
                        self.device_info.dt[dtype] = dt
                        self.device_info.BLOCK_SIZE[dtype] = BLOCK_SIZE
                    if dt < min_dt[dtype]:
                        min_dt[dtype] = dt
                    key = ("%s_%d_%d_%d" %
                           ("double2" if dtype[-1] == "2" else "double",
                            self.AB_WIDTH, self.B_HEIGHT, self.A_HEIGHT))
                    c = cc[key].copy()
                    c -= self.c.v
                    c = numpy.sqrt(numpy.square(numpy.real(c)) +
                                   numpy.square(numpy.imag(c)))
                    self.info(
                        "Avg is %.3f seconds, MSE = %.6f, "
                        "max_diff = %.6f" %
                        (dt, numpy.sum(c) / c.size, c.max()))
                    self._cleanup_after_test()
                except RuntimeError:
                    a, b, c = sys.exc_info()
                    self.info(
                        "Program compilation or run failed for "
                        "BLOCK_SIZE = %d and dtype = %s "
                        "(details in stderr)" % (BLOCK_SIZE, dtype))
                    traceback.print_exception(a, b, c)
                    self._cleanup_after_test()

        del cc

        for dtype in sorted(opencl_types.dtypes.keys()):
            self.info("Rating of numpy for dtype = %s: %.4f" % (
                dtype, min_dt[dtype] / dt_numpy[dtype]))
        for device_info in device_infos.values():
            for dtype in sorted(opencl_types.dtypes.keys()):
                self.info("================")
                self.info(dtype)
                rating = min_dt[dtype] / device_info.dt[dtype]
                try:
                    if device_info.rating[dtype] != rating:
                        if device_info.rating[dtype]:
                            self.info(
                                "UPD Rating(%s): %.4f" % (device_info.desc,
                                                          rating))
                        else:
                            self.info(
                                "NEW Rating(%s): %.4f" % (device_info.desc,
                                                          rating))
                    else:
                        self.info("Rating(%s): %.4f" % (device_info.desc,
                                                        rating))
                except:
                    self.exception()
                device_info.rating[dtype] = rating
                device_info.min_dt[dtype] = min_dt[dtype]
        self.info("================")

    def _prepare_test(self, BLOCK_SIZE, dtype, cc):
        self.AB_WIDTH = 3001
        self.B_HEIGHT = 3001
        self.A_HEIGHT = 3001
        # self.AB_WIDTH = formats.roundup(self.AB_WIDTH, BLOCK_SIZE)
        # self.B_HEIGHT = formats.roundup(self.B_HEIGHT, BLOCK_SIZE)
        # self.A_HEIGHT = formats.roundup(self.A_HEIGHT, BLOCK_SIZE)
        self.info("Matricies are: [%d, %d] * [%d, %d] = [%d, %d]" % (
            self.AB_WIDTH, self.A_HEIGHT, self.B_HEIGHT, self.AB_WIDTH,
            self.A_HEIGHT, self.B_HEIGHT))
        self.rnd_state = rnd.default.state

        xdtype = ("complex" if dtype in (numpy.complex64, numpy.complex128)
                  else "real")

        self.a = formats.Vector()
        self.a.v = numpy.zeros([self.A_HEIGHT, self.AB_WIDTH],
                               dtype=opencl_types.dtypes[dtype])
        a_rnd = cc.get("a_rnd")
        if a_rnd is None:
            a_rnd = {}
            cc["a_rnd"] = a_rnd
        if a_rnd.get(xdtype) is None:
            rnd.default.fill(self.a.v, -0.1, 0.1)
            a_rnd[xdtype] = self.a.v.copy()
        else:
            self.a.v[:] = a_rnd[xdtype][:]

        self.b = formats.Vector()
        self.b.v = numpy.zeros([self.B_HEIGHT, self.AB_WIDTH],
                               dtype=opencl_types.dtypes[dtype])
        b_rnd = cc.get("b_rnd")
        if b_rnd is None:
            b_rnd = {}
            cc["b_rnd"] = b_rnd
        if b_rnd.get(xdtype) is None:
            rnd.default.fill(self.b.v, -0.1, 0.1)
            b_rnd[xdtype] = self.b.v.copy()
        else:
            self.b.v[:] = b_rnd[xdtype][:]

        self.bias = formats.Vector()
        self.bias.v = numpy.zeros(self.B_HEIGHT,
                                  dtype=opencl_types.dtypes[dtype])
        bias_rnd = cc.get("bias_rnd")
        if bias_rnd is None:
            bias_rnd = {}
            cc["bias_rnd"] = bias_rnd
        if bias_rnd.get(xdtype) is None:
            rnd.default.fill(self.bias.v, -0.1, 0.1)
            bias_rnd[xdtype] = self.bias.v.copy()
        else:
            self.bias.v[:] = bias_rnd[xdtype][:]

        self.c = formats.Vector()
        self.c.v = numpy.zeros([self.A_HEIGHT, self.B_HEIGHT],
                               dtype=opencl_types.dtypes[dtype])

    def _cleanup_after_test(self):
        del(self.c)
        del(self.bias)
        del(self.b)
        del(self.a)
        rnd.default.state = self.rnd_state
        del(self.rnd_state)
        del(self.A_HEIGHT)
        del(self.B_HEIGHT)
        del(self.AB_WIDTH)

    def _do_cpu_test(self, cc, key):
        """Pure single core CPU test.
        """
        dtype = (
            numpy.complex128 if self.a.v.dtype in (
                numpy.complex64, numpy.complex128) else numpy.float64)
        a = numpy.empty(self.a.v.shape, dtype=dtype)
        a[:] = self.a.v[:]
        bt = self.b.v.transpose()
        b = numpy.empty(bt.shape, dtype=dtype)
        b[:] = bt[:]
        bias = numpy.empty(self.bias.v.shape, dtype=dtype)
        bias[:] = self.bias.v[:]
        c = numpy.empty(self.c.v.shape, dtype=dtype)
        t1 = time.time()
        numpy.dot(a, b, c)
        c[:] += bias
        c *= 0.6666
        numpy.tanh(c, c)
        c *= 1.7159
        dt = time.time() - t1
        cc[key] = c
        return dt

    def _do_test(self, BLOCK_SIZE, dtype, iters):
        """Do test for specific context.
        """

        class WorkflowStub(units.Unit):
            def __init__(self):
                super(WorkflowStub, self).__init__(self)

            def add_ref(self, unit):
                pass

            def del_ref(self, unit):
                pass

        obj = units.OpenCLUnit(WorkflowStub(), device=self)
        obj.cl_sources_["forward.cl"] = {}
        defines = {
            "ACTIVATION_TANH": 1,
            "BLOCK_SIZE": BLOCK_SIZE,
            "H": self.AB_WIDTH,
            "Y": self.B_HEIGHT,
            "BATCH": self.A_HEIGHT}
        obj.build_program(defines, os.path.join(root.common.cache_dir,
                                                "test.cl"),
                          dtype=dtype)

        krn = obj.get_kernel("feed_layer")

        self.a.initialize(self)
        self.b.initialize(self)
        self.c.initialize(self)
        self.bias.initialize(self)

        krn.set_arg(0, self.a.v_)
        krn.set_arg(1, self.b.v_)
        krn.set_arg(2, self.c.v_)
        krn.set_arg(3, self.bias.v_)

        global_size = [formats.roundup(self.B_HEIGHT, BLOCK_SIZE),
                       formats.roundup(self.A_HEIGHT, BLOCK_SIZE)]
        local_size = [BLOCK_SIZE, BLOCK_SIZE]
        t1 = time.time()
        # Will skip the first iteration
        ev = None
        for i in range(iters + 1):
            ev = self.queue_.execute_kernel(
                krn, global_size, local_size,
                wait_for=(None if ev is None else (ev,)))
            if i == 0:
                self.queue_.flush()
                ev.wait()
                ev = None
                t1 = time.time()
        self.queue_.flush()
        ev.wait()
        dt = time.time() - t1
        # Get results back
        self.c.map_read()
        return dt / iters
