#
# This file is part of LiteX.
#
# Copyright (c) 2017-2018 William D. Jones <thor0505@comcast.net>
# Copyright (c) 2019 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause


import os
import sys
import subprocess
from shutil import which

from migen.fhdl.structure import _Fragment

from litex.build.generic_platform import *
from litex.build import tools
from litex.build.lattice import common

# IO Constraints (.pcf) ----------------------------------------------------------------------------

def _build_pcf(named_sc, named_pc):
    r = ""
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for bit, pin in enumerate(pins):
                r += "set_io {}[{}] {}\n".format(sig, bit, pin)
        else:
            r += "set_io {} {}\n".format(sig, pins[0])
    if named_pc:
        r += "\n" + "\n\n".join(named_pc)
    return r

# Timing Constraints (in pre_pack file) ------------------------------------------------------------

def _build_pre_pack(vns, clocks):
    r = ""
    for clk, period in clocks.items():
        r += """ctx.addClock("{}", {})\n""".format(vns.get_name(clk), 1e3/period)
    return r

# Yosys/Nextpnr Helpers/Templates ------------------------------------------------------------------

_yosys_template = [
    "verilog_defaults -push",
    "verilog_defaults -add -defer",
    "{read_files}",
    "verilog_defaults -pop",
    "attrmap -tocase keep -imap keep=\"true\" keep=1 -imap keep=\"false\" keep=0 -remove keep=0",
    "synth_ice40 {synth_opts} -json {build_name}.json -top {build_name} -dsp",
]

def _yosys_import_sources(platform):
    includes = ""
    reads = []
    for path in platform.verilog_include_paths:
        includes += " -I" + path
    for filename, language, library in platform.sources:
        # yosys has no such function read_systemverilog
        if language == "systemverilog":
            language = "verilog -sv"
        reads.append("read_{}{} {}".format(
            language, includes, filename))
    return "\n".join(reads)

def _build_yosys(template, platform, build_name, synth_opts):
    ys = []
    for l in template:
        ys.append(l.format(
            build_name = build_name,
            read_files = _yosys_import_sources(platform),
            synth_opts = synth_opts
        ))
    tools.write_to_file(build_name + ".ys", "\n".join(ys))

def parse_device(device):
    packages = {
        "lp384": ["qn32", "cm36", "cm49"],
        "lp1k": ["swg16tr", "cm36", "cm49", "cm81", "cb81", "qn84", "cm121", "cb121"],
        "hx1k": ["vq100", "cb132", "tq144"],
        "lp8k": ["cm81", "cm81:4k", "cm121", "cm121:4k", "cm225", "cm225:4k"],
        "hx8k": ["bg121", "bg121:4k", "cb132", "cb132:4k", "cm121",
                 "cm121:4k", "cm225", "cm225:4k", "cm81", "cm81:4k",
                 "ct256", "tq144:4k"],
        "up3k": ["sg48", "uwg30"],
        "up5k": ["sg48", "uwg30"],
        "u4k": ["sg48"],
    }

    (family, architecture, package) = device.split("-")
    if family not in ["ice40"]:
        raise ValueError("Unknown device family {}".format(family))
    if architecture not in packages.keys():
        raise ValueError("Invalid device architecture {}".format(architecture))
    if package not in packages[architecture]:
        raise ValueError("Invalid device package {}".format(package))
    return (family, architecture, package)

# Script -------------------------------------------------------------------------------------------

_build_template = [
    "yosys -l {build_name}.rpt {build_name}.ys",
    "nextpnr-ice40 --json {build_name}.json --pcf {build_name}.pcf --asc {build_name}.txt \
    --pre-pack {build_name}_pre_pack.py --{architecture} --package {package} {timefailarg} {ignoreloops} --seed {seed}",
    "icepack -s {build_name}.txt {build_name}.bin"
]

def _build_script(build_template, build_name, architecture, package, timingstrict, ignoreloops, seed):
    if sys.platform in ("win32", "cygwin"):
        script_ext = ".bat"
        script_contents = "@echo off\nrem Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n\n"
        fail_stmt = " || exit /b"
    else:
        script_ext = ".sh"
        script_contents = "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\nset -e\n"
        fail_stmt = ""

    for s in build_template:
        s_fail = s + "{fail_stmt}\n"  # Required so Windows scripts fail early.
        script_contents += s_fail.format(
            build_name   = build_name,
            architecture = architecture,
            package      = package,
            timefailarg  = "--timing-allow-fail" if not timingstrict else "",
            ignoreloops  = "--ignore-loops" if ignoreloops else "",
            fail_stmt    = fail_stmt,
            seed         = seed)

    script_file = "build_" + build_name + script_ext
    tools.write_to_file(script_file, script_contents, force_unix=False)

    return script_file

def _run_script(script):
    if sys.platform in ("win32", "cygwin"):
        shell = ["cmd", "/c"]
    else:
        shell = ["bash"]

    if which("yosys") is None or which("nextpnr-ice40") is None:
        msg = "Unable to find Yosys/Nextpnr toolchain, please:\n"
        msg += "- Add Yosys/Nextpnr toolchain to your $PATH."
        raise OSError(msg)

    if subprocess.call(shell + [script]) != 0:
        raise OSError("Error occured during Yosys/Nextpnr's script execution.")

# LatticeIceStormToolchain -------------------------------------------------------------------------

class LatticeIceStormToolchain:
    attr_translate = {
        "keep": ("keep", "true"),
    }

    special_overrides = common.lattice_ice40_special_overrides

    def __init__(self):
        self.yosys_template = _yosys_template
        self.build_template = _build_template
        self.clocks         = dict()

    def build(self, platform, fragment,
        build_dir      = "build",
        build_name     = "top",
        synth_opts     = "",
        run            = True,
        timingstrict   = False,
        ignoreloops    = False,
        seed           = 1,
        **kwargs):

        # Create build directory
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        # Finalize design
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)

        # Generate verilog
        v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(v_output.ns)
        v_file = build_name + ".v"
        v_output.write(v_file)
        platform.add_source(v_file)

        # Generate design io constraints file (.pcf)
        tools.write_to_file(build_name + ".pcf",_build_pcf(named_sc, named_pc))

        # Generate design timing constraints file (in pre_pack file)
        tools.write_to_file(build_name + "_pre_pack.py", _build_pre_pack(v_output.ns, self.clocks))

        # Generate Yosys script
        _build_yosys(self.yosys_template, platform, build_name, synth_opts=synth_opts)

        # Translate device to Nextpnr architecture/package
        (family, architecture, package) = parse_device(platform.device)

        # Generate build script
        script = _build_script(self.build_template, build_name, architecture, package, timingstrict, ignoreloops, seed)

        # Run
        if run:
            _run_script(script)

        os.chdir(cwd)

        return v_output.ns

    def add_period_constraint(self, platform, clk, period):
        clk.attr.add("keep")
        if clk in self.clocks:
            if period != self.clocks[clk]:
                raise ValueError("Clock already constrained to {:.2f}ns, new constraint to {:.2f}ns"
                    .format(self.clocks[clk], period))
        self.clocks[clk] = period

def icestorm_args(parser):
    toolchain_group = parser.add_argument_group("toolchain")
    toolchain_group.add_argument("--nextpnr-timingstrict", action="store_true", help="Make the build fail when Timing is not met.")
    toolchain_group.add_argument("--nextpnr-ignoreloops",  action="store_true", help="Use strict Timing mode (Build will fail when Timings are not met).")
    toolchain_group.add_argument("--nextpnr-seed",         default=1, type=int, help="Set Nextpnr's seed.")

def icestorm_argdict(args):
    return {
        "timingstrict": args.nextpnr_timingstrict,
        "ignoreloops":  args.nextpnr_ignoreloops,
        "seed":         args.nextpnr_seed,
    }
