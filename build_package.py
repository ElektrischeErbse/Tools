#!/usr/bin/env python3
"""
一键编译 + FPM打包工具

本脚本用于自动化构建 CMake/Autotools 项目，并打包为 deb 格式。

【前置依赖】
本脚本依赖 fpm (Effing Package Management) 工具，而 fpm 依赖 Ruby 环境。

. 安装 Ruby：
   - macOS (推荐 rbenv): brew install rbenv && rbenv install 3.3.0
   - Linux (Ubuntu/Debian): sudo apt update && sudo apt install ruby-full
   - Windows: 前往 https://rubyinstaller.org/ 下载 Ruby+Devkit 安装

. 安装 fpm：
   - 国内网络建议先换源: gem sources --add https://mirrors.tuna.tsinghua.edu.cn/rubygems/ --remove https://rubygems.org/
   - 执行安装: gem install fpm

【用法示例】
   python build_deb.py --name mylib --version 1.0.0
"""

import os
import shutil
import subprocess
import argparse
import re
import shlex
from typing import List, Optional, cast


def run_cmd(
    cmd: List[str], cwd: Optional[str] = None, capture_output: bool = False
) -> Optional[str]:
    """
    执行外部命令。
    :param cmd: 命令及参数列表
    :param cwd: 工作目录
    :param capture_output: 是否捕获标准输出
    :return: 如果 capture_output 为 True，返回输出字符串，否则返回 None
    """
    print(f"> RUN: {' '.join(cmd)}")
    if capture_output:
        result = subprocess.check_output(cmd, cwd=cwd, text=True)
        return result
    subprocess.check_call(cmd, cwd=cwd)
    return None


def detect_build_system(src_dir: str) -> str:
    """
    检测源码目录使用的构建系统。
    :return: 'cmake' 或 'autotools'
    """
    cmake_file = os.path.join(src_dir, "CMakeLists.txt")
    configure_ac = os.path.join(src_dir, "configure.ac")
    configure_sh = os.path.join(src_dir, "configure")

    if os.path.exists(cmake_file):
        return "cmake"
    if os.path.exists(configure_ac) or os.path.exists(configure_sh):
        return "autotools"
    raise RuntimeError(
        "无法识别构建系统！缺少 CMakeLists.txt 或 configure.ac/configure"
    )


def find_bin_lib_files(stage_prefix: str) -> List[str]:
    """
    在 stage 目录中查找可执行文件和 .so 库文件。
    :return: 找到的文件绝对路径列表
    """
    scan_paths = [os.path.join(stage_prefix, "bin"), os.path.join(stage_prefix, "lib")]
    targets: List[str] = []
    for p in scan_paths:
        if not os.path.isdir(p):
            continue
        for root, _, files in os.walk(p):
            for fn in files:
                full_path = os.path.join(root, fn)
                if fn.endswith(".so") or os.access(full_path, os.X_OK):
                    targets.append(full_path)
    return targets


def get_deps_from_ldd(filepath: str) -> List[str]:
    """
    使用 ldd 获取文件的动态链接库依赖。
    :return: 依赖的 so 库名称列表
    """
    out = run_cmd(["ldd", filepath], capture_output=True) or ""
    lib_names: List[str] = []
    pattern = re.compile(r"(lib[^ ]+\.so[^ ]*)")
    for line in out.splitlines():
        if "linux-vdso.so" in line or "ld-linux-x86-64.so" in line:
            continue
        m = pattern.search(line)
        if m:
            lib_names.append(m.group(1))
    return lib_names


def resolve_soname_to_pkg(soname: str) -> Optional[str]:
    """
    通过 dpkg -S 将 so 库名解析为 Debian 包名。
    :return: 包名，如果未找到则返回 None
    """
    try:
        out = run_cmd(["dpkg", "-S", soname], capture_output=True) or ""
        pkg = out.split(":")[0].strip()
        return pkg
    except subprocess.CalledProcessError:
        return None


def auto_collect_dependencies(stage_prefix: str) -> List[str]:
    """
    自动扫描并收集 stage 目录中二进制文件的依赖包。
    :return: 去重并排序后的依赖包名列表
    """
    files = find_bin_lib_files(stage_prefix)
    sonames: set[str] = set()
    for f in files:
        print(f"\n ldd scan: {f}")
        s_list = get_deps_from_ldd(f)
        for s in s_list:
            sonames.add(s)

    pkgs: set[str] = set()
    for soname in sonames:
        pkg = resolve_soname_to_pkg(soname)
        if pkg:
            pkgs.add(pkg)

    return sorted(list(pkgs))


# 定义一个类来专门描述命令行参数，方便 IDE 补全
class BuildArgs(argparse.Namespace):
    name: str
    version: str
    src: str
    depends: str
    no_auto_dep: bool
    cmake_args: str
    configure_args: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一键编译 + FPM打包deb(安装至/opt)，支持CMake/Autotools，自动ldd收集依赖"
    )
    parser.add_argument("--name", required=True, help="deb包名，小写，如 mylib")
    parser.add_argument("--version", default="1.0.0", help="版本号，默认1.0.0")
    parser.add_argument("--src", default=".", help="源码目录，默认当前目录")
    parser.add_argument(
        "--depends", default="", help="手动追加依赖，多个逗号分隔，可选"
    )
    parser.add_argument(
        "--no-auto-dep", action="store_true", help="关闭自动依赖扫描，只用手动--depends"
    )
    parser.add_argument(
        "--cmake-args",
        default="",
        help='CMake额外参数，使用 --cmake-args="-DXXX=ON"，不要写CMAKE_INSTALL_PREFIX',
    )
    parser.add_argument(
        "--configure-args",
        default="",
        help='./configure额外参数，使用 --configure-args="--enable-shared"，不要写--prefix',
    )

    # 明确指定 args 的类型为我们定义的 BuildArgs
    args = cast(BuildArgs, parser.parse_args())

    src_dir: str = os.path.abspath(args.src)
    pkg_name: str = args.name
    pkg_ver: str = args.version
    stage_root: str = "/tmp/stage"
    opt_install_path: str = f"opt/{pkg_name}"
    stage_prefix: str = os.path.join(stage_root, opt_install_path)

    if shutil.which("fpm") is None:
        raise RuntimeError("fpm 未安装，请先安装：gem install fpm")

    build_type: str = detect_build_system(src_dir)
    print(f"\n 检测构建系统: {build_type}")

    if os.path.exists(stage_root):
        shutil.rmtree(stage_root)
    os.makedirs(stage_root, exist_ok=True)

    if build_type == "cmake":
        build_dir: str = os.path.join(src_dir, "build")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir)
        cmake_cmd: List[str] = ["cmake", "..", f"-DCMAKE_INSTALL_PREFIX={stage_prefix}"]
        if args.cmake_args.strip():
            extra_cmake: List[str] = shlex.split(args.cmake_args)
            cmake_cmd.extend(extra_cmake)
        run_cmd(cmake_cmd, cwd=build_dir)
        run_cmd(["make", "-j", str(os.cpu_count())], cwd=build_dir)
        run_cmd(["make", "install"], cwd=build_dir)

    elif build_type == "autotools":
        configure_sh: str = os.path.join(src_dir, "configure")
        if not os.path.exists(configure_sh):
            print("autotools: 未找到configure，执行autoreconf -ivf")
            run_cmd(["autoreconf", "-ivf"], cwd=src_dir)
        conf_cmd: List[str] = ["./configure", f"--prefix={stage_prefix}"]
        if args.configure_args.strip():
            extra_conf: List[str] = shlex.split(args.configure_args)
            conf_cmd.extend(extra_conf)
        run_cmd(conf_cmd, cwd=src_dir)
        run_cmd(["make", "-j", str(os.cpu_count())], cwd=src_dir)
        run_cmd(["make", "install"], cwd=src_dir)

    # 收集依赖
    dep_list: List[str] = []
    if not args.no_auto_dep:
        auto_deps: List[str] = auto_collect_dependencies(stage_prefix)
        print(f"\n 自动扫描得到依赖: {auto_deps}")
        dep_list.extend(auto_deps)
    if args.depends.strip():
        manual_deps: List[str] = [
            x.strip() for x in args.depends.split(",") if x.strip()
        ]
        print(f" 手动追加依赖: {manual_deps}")
        dep_list.extend(manual_deps)

    dep_list = sorted(list(set(dep_list)))
    final_dep_str: str = ",".join(dep_list)
    print(f"\n 最终deb依赖列表: {final_dep_str}")

    # ================= 新增：自动生成环境变量配置脚本 =================
    profile_d_dir = os.path.join(stage_root, "etc", "profile.d")
    os.makedirs(profile_d_dir, exist_ok=True)
    env_script_path = os.path.join(profile_d_dir, f"{pkg_name}.sh")

    # 探测 stage 目录中实际存在哪些子目录
    bin_path = os.path.join(stage_prefix, "bin")
    include_path = os.path.join(stage_prefix, "include")
    lib_path = os.path.join(stage_prefix, "lib")
    cmake_path = os.path.join(stage_prefix, "lib", "cmake")

    env_lines = [
        f"# Auto-generated environment variables for {pkg_name}",
        f"# Generated by fpm-packager script",
        "",
    ]

    # 1. 如果有 bin 目录，添加到 PATH
    if os.path.isdir(bin_path):
        env_lines.append(f'export PATH="/opt/{pkg_name}/bin:$PATH"')

    # 2. 如果有 include 目录，配置 GCC/G++ 头文件查找路径
    if os.path.isdir(include_path):
        env_lines.append(
            f'export C_INCLUDE_PATH="/opt/{pkg_name}/include:$C_INCLUDE_PATH"'
        )
        env_lines.append(
            f'export CPLUS_INCLUDE_PATH="/opt/{pkg_name}/include:$CPLUS_INCLUDE_PATH"'
        )

    # 3. 如果有 lib 目录，配置编译时库查找路径和运行时动态库路径
    if os.path.isdir(lib_path):
        env_lines.append(f'export LIBRARY_PATH="/opt/{pkg_name}/lib:$LIBRARY_PATH"')
        env_lines.append(
            f'export LD_LIBRARY_PATH="/opt/{pkg_name}/lib:$LD_LIBRARY_PATH"'
        )

    # 4. 如果有 lib/cmake 目录，配置 CMake find_package 查找路径
    if os.path.isdir(cmake_path):
        env_lines.append(
            f'export CMAKE_PREFIX_PATH="/opt/{pkg_name}:$CMAKE_PREFIX_PATH"'
        )

    # 将配置写入脚本文件
    with open(env_script_path, "w") as f:
        f.write("\n".join(env_lines) + "\n")
    print(f"📝 已生成环境变量脚本: {env_script_path}")

    # ================= 修改 FPM 打包命令，加入 etc 目录 =================
    fpm_cmd: List[str] = [
        "fpm",
        "-s",
        "dir",
        "-t",
        "deb",
        "-n",
        pkg_name,
        "-v",
        pkg_ver,
        "-a",
        "amd64",
        "--iteration",
        "1",
        "--depends",
        final_dep_str,
        "--description",
        f"Custom build {pkg_name}, installed to /opt/{pkg_name}",
        "--maintainer",
        "dev@example.com",
        "-C",
        stage_root,
        "opt",
        "etc",  # 👈 新增：将生成的 etc/profile.d 目录一并打包进 deb
    ]
    run_cmd(fpm_cmd, cwd=src_dir)
    print(f"\n🎉 打包完成！deb包生成在源码目录: {src_dir}")


if __name__ == "__main__":
    main()
