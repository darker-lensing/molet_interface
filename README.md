# molet_interface

`molet_interface` provides the `MoletInterface` Python class for working with [MOLET](https://github.com/gvernard/molet) from Python.

This repository contains the Python interface. The original MOLET source code and its documentation are maintained in the [upstream MOLET repository](https://github.com/gvernard/molet).

## Upstream project and dependencies

- [MOLET — original source code and documentation](https://github.com/gvernard/molet)
- [gerlumphpp](https://github.com/gvernard/gerlumphpp)
- [vkl_lib](https://github.com/gvernard/vkl_lib)
- [CCfits](https://heasarc.gsfc.nasa.gov/fitsio/CCfits/)

## Installation

Create the shared Conda environment and install the Python package first, then build MOLET and its libraries as described below.

### Platform and version

These instructions target **macOS Tahoe 26.3 on Apple M5** and were prepared for the **2023 version of MOLET**. The Linux alternatives target Ubuntu/Debian with GCC.

The commands clone the upstream repositories without selecting a specific commit. To reproduce a particular version, check out the corresponding commit before applying the patches.

### Before you begin

- Install [Miniconda](https://www.anaconda.com/download) and ensure that `conda` is available in your terminal.
- This guide locates MOLET at `~/git_repos/molet`. If you choose another location, update all corresponding paths.
- Keep the `molet` Conda environment active whenever you build or run MOLET, including when using it through `molet_interface`.

In a new terminal session, after the following installation, activate the environment with:

```bash
conda activate molet
```

### 1. Create the Conda environment

```bash
conda create -n molet python=3.11 -y
conda activate molet
```

Install the required libraries and build tools:

```bash
conda install -c conda-forge \
  fftw cfitsio libpng sqlite gmp mpfr jsoncpp cmake \
  libboost libboost-devel cgal gfortran \
  autoconf automake libtool pkg-config git jq \
  pip setuptools wheel numpy matplotlib astropy ipython jupyter \
  -y
```

`jq` is a command-line JSON processor used by MOLET's driver to read configuration values and map IDs.

### 2. Install molet_interface

Clone this interface repository if you have not already done so:

```bash
mkdir -p "$HOME/git_repos"
cd "$HOME/git_repos"
git clone https://github.com/darker-lensing/molet_interface.git
```

Then install it into the active environment:

```bash
cd "$HOME/git_repos/molet_interface"
conda activate molet
pip install .
python -c "from molet_interface import MoletInterface; print('Import successful')"
```

Installing the interface before MOLET is supported, but running simulations requires the remaining steps. After installation, notebooks can be opened in this environment with `jupyter notebook`.

### 3. Install system build tools

**macOS**

```bash
xcode-select --install
```

Wait for the installation to finish before continuing. If the tools are already installed, proceed to the next step.

**Linux — Ubuntu/Debian**

```bash
sudo apt update
sudo apt install -y build-essential curl
```

On other Linux distributions, install the equivalent compiler and build tools using the system package manager.

### 4. Clone MOLET at $HOME/git_repos/molet

```bash
mkdir -p "$HOME/git_repos"
cd "$HOME/git_repos"
git clone https://github.com/gvernard/molet.git
```

Create a separate directory for third-party source code:

```bash
mkdir -p "$HOME/molet_thirdparty/src"
```

### 5. Build and install CCfits

Download and extract CCfits 2.5:

```bash
cd "$HOME/molet_thirdparty/src"

curl -L -O \
  https://heasarc.gsfc.nasa.gov/fitsio/CCfits-2.5/CCfits-2.5.tar.gz

tar -xzf CCfits-2.5.tar.gz
cd CCfits
```

Configure, build, and install it into the active Conda environment:

**macOS**

```bash
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  CXXFLAGS="-I$CONDA_PREFIX/include -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION -Wno-deprecated-declarations"

make -j4
make install
```

**Linux (Bash for shell setup)**

```bash
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  CXXFLAGS="-I$CONDA_PREFIX/include"

make -j4
make install
```

### 6. Build and install gerlumphpp

```bash
cd "$HOME/molet_thirdparty/src"
git clone https://github.com/gvernard/gerlumphpp.git
cd gerlumphpp

mkdir -p maps
autoreconf -i
```

The map path below, in `--with-map-path`, follows the folder structure adopted in this guide. Keep the trailing slash: the library concatenates this path with each map ID. Creating the directory does not download maps. To change this compiled-in path later, reconfigure and rebuild gerlumphpp.

**macOS**

```bash
CXXFLAGS="-g -O2 -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION" \
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-map-path="$HOME/molet_thirdparty/src/gerlumphpp/maps/" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-png="$CONDA_PREFIX" \
  --with-fftw3="$CONDA_PREFIX" \
  --enable-gpu=no

make -j4
make install
```

**Linux (Bash for shell setup)**

```bash
CXXFLAGS="-g -O2" \
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-map-path="$HOME/molet_thirdparty/src/gerlumphpp/maps/" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-png="$CONDA_PREFIX" \
  --with-fftw3="$CONDA_PREFIX" \
  --enable-gpu=no

make -j4
make install
```

### 7. Patch, build, and install vkl_lib

```bash
cd "$HOME/molet_thirdparty/src"
git clone https://github.com/gvernard/vkl_lib.git
cd vkl_lib
```

Apply the compatibility patches for CGAL linking and virtual destructors.

> Apply these patches only once to a fresh checkout. The `sed -i ''` syntax used here is specific to macOS.

**macOS**

```bash
sed -i '' \
  's/ac_new_LIBS+=" -lCGAL"/ac_new_LIBS+=""/' \
  configure.ac

sed -i '' \
  's/libvkl_la_LIBADD = -lgfortran -lcfitsio -lCCfits -lgmp -lCGAL -ljsoncpp/libvkl_la_LIBADD = -lgfortran -lcfitsio -lCCfits -lgmp -ljsoncpp/' \
  Makefile.am

sed -i '' \
  's/~Base/virtual ~Base/g' \
  include/*.hpp
```

**Linux (Bash for shell setup)**

```bash
sed -i \
  's/ac_new_LIBS+=" -lCGAL"/ac_new_LIBS+=""/' \
  configure.ac

sed -i \
  's/libvkl_la_LIBADD = -lgfortran -lcfitsio -lCCfits -lgmp -lCGAL -ljsoncpp/libvkl_la_LIBADD = -lgfortran -lcfitsio -lCCfits -lgmp -ljsoncpp/' \
  Makefile.am

sed -i \
  's/~Base/virtual ~Base/g' \
  include/*.hpp
```

Configure, build, and install:

**macOS**

```bash
autoreconf -i

CXXFLAGS="-g -O2 -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION -DCGAL_DISABLE_ROUNDING_MATH_CHECK" \
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-gmp="$CONDA_PREFIX" \
  --with-CGAL="$CONDA_PREFIX" \
  --with-jsoncpp="$CONDA_PREFIX"

make -j4
make install
```

**Linux (Bash for shell setup)**

```bash
autoreconf -i

CXXFLAGS="-g -O2" \
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-gmp="$CONDA_PREFIX" \
  --with-CGAL="$CONDA_PREFIX" \
  --with-jsoncpp="$CONDA_PREFIX"

make -j4
make install
```

### 8. Patch, build, and install MOLET

```bash
cd "$HOME/git_repos/molet"
```

Apply the patches for noise initialization, cleanup, virtual destructors, and CGAL linking.

> Apply these patches only once to a fresh checkout.

**macOS**

```bash
sed -i '' \
  's/~BaseNoise()/virtual ~BaseNoise()/g' \
  instruments/include/*.hpp

sed -i '' \
  's/this->texp = texp;/this->texp = texp; this->noise_realization = nullptr;/g' \
  instruments/src/noise.cpp

sed -i '' \
  's/delete(noise_realization);/if (noise_realization) { delete noise_realization; noise_realization = nullptr; }/g' \
  instruments/src/noise.cpp

sed -i '' \
  's/-lCGAL //g' \
  Makefile.am

sed -i '' \
  's/ac_new_LIBS+=" -lCGAL"/ac_new_LIBS+=""/' \
  configure.ac
```

**Linux (Bash for shell setup)**

```bash
sed -i \
  's/~BaseNoise()/virtual ~BaseNoise()/g' \
  instruments/include/*.hpp

sed -i \
  's/this->texp = texp;/this->texp = texp; this->noise_realization = nullptr;/g' \
  instruments/src/noise.cpp

sed -i \
  's/delete(noise_realization);/if (noise_realization) { delete noise_realization; noise_realization = nullptr; }/g' \
  instruments/src/noise.cpp

sed -i \
  's/-lCGAL //g' \
  Makefile.am

sed -i \
  's/ac_new_LIBS+=" -lCGAL"/ac_new_LIBS+=""/' \
  configure.ac
```

Configure, build, and install:

**macOS**

```bash
autoreconf -i

CXXFLAGS="-g -O2 -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION -DCGAL_DISABLE_ROUNDING_MATH_CHECK" \
./configure \
  --with-jq="$CONDA_PREFIX" \
  --with-fftw3="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-gmp="$CONDA_PREFIX" \
  --with-CGAL="$CONDA_PREFIX" \
  --with-jsoncpp="$CONDA_PREFIX" \
  --with-png="$CONDA_PREFIX" \
  --with-sqlite3="$CONDA_PREFIX" \
  --with-vkl="$CONDA_PREFIX" \
  --with-gerlumph="$CONDA_PREFIX"

make -j4
make install
```

**Linux (Bash for shell setup)**

```bash
autoreconf -i

CXXFLAGS="-g -O2" \
./configure \
  --with-jq="$CONDA_PREFIX" \
  --with-fftw3="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-gmp="$CONDA_PREFIX" \
  --with-CGAL="$CONDA_PREFIX" \
  --with-jsoncpp="$CONDA_PREFIX" \
  --with-png="$CONDA_PREFIX" \
  --with-sqlite3="$CONDA_PREFIX" \
  --with-vkl="$CONDA_PREFIX" \
  --with-gerlumph="$CONDA_PREFIX"

make -j4
make install
```

### 9. Configure runtime library paths

Add the Conda and MOLET library directories to the compiled executables:

**macOS**

```bash
cd "$HOME/git_repos/molet/bin"

for f in *; do
  if file "$f" | grep -q "Mach-O"; then
    install_name_tool -add_rpath "$CONDA_PREFIX/lib" "$f"
    install_name_tool -add_rpath "$HOME/git_repos/molet/lib" "$f"
  fi
done

cd ..
```

**Linux**

Skip this block. MOLET configure adds runtime paths for dependencies. If a shared library cannot be found, inspect the executable with `ldd`; for the paths in this guide, a session-level diagnostic fallback is:

```bash
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:$HOME/git_repos/molet/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

If a runtime path is already present, `install_name_tool` may report a duplicate-path error for that entry.

### 10. Configure your shell and initialize the instrument database

Add MOLET to your shell's executable search path. Run the following append command once:

**macOS**

```bash
echo 'export PATH="$HOME/git_repos/molet/bin:$PATH"' >> "$HOME/.zshrc"
source "$HOME/.zshrc"
conda activate molet
```

**Linux (Bash for shell setup)**

```bash
echo 'export PATH="$HOME/git_repos/molet/bin:$PATH"' >> "$HOME/.bashrc"
source "$HOME/.bashrc"
conda activate molet
```

Initialize the instrument database:

```bash
cd "$HOME/git_repos/molet"
./data/initialize_instruments.sh "$HOME/git_repos/molet"
```

### 11. Run a test

With the Conda environment active, run the provided test from the MOLET repository:

```bash
conda activate molet
cd "$HOME/git_repos/molet"

jq --version
./bin/check_get_map_path
molet_driver tests/general/test_D/molet_input.json
```
The map-path check must end in `/gerlumphpp/maps/`. The static test does not validate microlensing or GPU execution.

## GERLUMPH maps

The directory structure is:

```text
~/molet_thirdparty/src/gerlumphpp/maps/
└── <map_id>/
    ├── map.bin
    └── mapmeta.dat
```

MOLET uses `data/gerlumph.db` to select map IDs from macro-image lensing properties. Download the required maps separately. See the [upstream map documentation](https://github.com/gvernard/molet#note-on-using-magnification-maps).

The map path is compiled into gerlumphpp, not configured by a Python default. To change it, repeat the gerlumphpp configure command with the new absolute path and trailing slash, then run `make clean`, `make -j4`, and `make install`.

Map-based microlensing requires a complete lens and compact-matter prescription, point source, instruments and observing times, intrinsic variability, and an explicit map-based `set_extrinsic_variability(...)` configuration. Omitting extrinsic variability when intrinsic variability is configured produces unity extrinsic curves, representing no microlensing.

## Optional CUDA acceleration

**macOS / Apple silicon:** keep `--enable-gpu=no`. CUDA cannot use the Apple GPU. CPU execution supports microlensing.

**Linux / compatible NVIDIA GPU:** CUDA can accelerate map convolutions. Install a compatible NVIDIA driver, CUDA Toolkit (including `nvcc` and cuFFT development libraries), and a supported host compiler using the [NVIDIA Linux installation guide](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/contents.html). Compiler and library paths must be available during building and execution.

Check the driver and compiler:

```bash
nvidia-smi
nvcc --version
```

`nvidia-smi` alone does not confirm that the toolkit is installed. In the Linux gerlumphpp configure command, replace `--enable-gpu=no` with `--enable-gpu=yes`, then build and install. When changing an existing build, run `make clean` after configuration and before rebuilding.

The [gerlumphpp README](https://github.com/gvernard/gerlumphpp#prerequisites) reports historical testing with CUDA 11.7 and a problem with 11.5. This does not guarantee compatibility with the newest toolkit or modern GPUs. Validate the selected GPU, driver, toolkit, compiler, and source combination with a map-based simulation; a static test does not exercise CUDA.

## Python dependencies

Python dependencies are installed by pip; native MOLET libraries are installed separately through the procedure above. Jupyter is available through the Conda environment or the optional `notebooks` extra (`pip install ".[notebooks]"`).

The CGAL linking patches target header-only CGAL installations. Apply all source patches once to a fresh compatible checkout; upstream changes may require reviewing them.
