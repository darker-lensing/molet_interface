# molet_interface

`molet_interface` provides the `MoletInterface` Python class for working with [MOLET](https://github.com/gvernard/molet) from Python.

This repository contains the Python interface. The original MOLET source code and its documentation are maintained in the [upstream MOLET repository](https://github.com/gvernard/molet).

## Upstream project and dependencies

- [MOLET — original source code and documentation](https://github.com/gvernard/molet)
- [gerlumphpp](https://github.com/gvernard/gerlumphpp)
- [vkl_lib](https://github.com/gvernard/vkl_lib)
- [CCfits](https://heasarc.gsfc.nasa.gov/fitsio/CCfits/)

## Install MOLET

Before using `molet_interface`, install MOLET and its required libraries. The following guide covers building the underlying MOLET software; installation and usage instructions for the Python package are provided separately.

### Platform and version

These instructions target **macOS Tahoe 26.3 on Apple M5** and were prepared for the **2023 version of MOLET**. They may work also on other machines and operating systems.

The commands clone the upstream repositories without selecting a specific commit. To reproduce a particular 2023 version, check out the corresponding commit before applying the patches.

### Before you begin

- Install [Miniconda](https://www.anaconda.com/download) and ensure that `conda` is available in your terminal.
- This guide assumes that MOLET is located at `~/git_repos/molet`. If you choose another location, update all corresponding paths.
- Keep the `molet` Conda environment active whenever you build or run MOLET, including when using it through `MoletInterface`.

In a new terminal session, after the following installation, activate the environment with:

```bash
conda activate molet
```

### 1. Install Xcode Command Line Tools

```bash
xcode-select --install
```

Wait for the installation to finish before continuing. If the tools are already installed, proceed to the next step.

### 2. Create the Conda environment

```bash
conda create -n molet python=3.11 -y
conda activate molet
```

Install the required libraries and build tools:

```bash
conda install -c conda-forge \
  fftw cfitsio libpng sqlite gmp mpfr jsoncpp cmake \
  libboost libboost-devel cgal gfortran \
  autoconf automake libtool pkg-config git \
  pip matplotlib jupyter \
  -y
```

### 3. Clone MOLET at $HOME/git_repos/molet

```bash
mkdir -p "$HOME/git_repos"
cd "$HOME/git_repos"
git clone https://github.com/gvernard/molet.git
```

Create a separate directory for third-party source code:

```bash
mkdir -p "$HOME/molet_thirdparty/src"
```

### 4. Build and install CCfits

Download and extract CCfits 2.5:

```bash
cd "$HOME/molet_thirdparty/src"

curl -L -O \
  https://heasarc.gsfc.nasa.gov/fitsio/CCfits-2.5/CCfits-2.5.tar.gz

tar -xzf CCfits-2.5.tar.gz
cd CCfits
```

Configure, build, and install it into the active Conda environment:

```bash
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-cfitsio="$CONDA_PREFIX" \
  CXXFLAGS="-I$CONDA_PREFIX/include -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION -Wno-deprecated-declarations"

make -j4
make install
```

### 5. Build and install gerlumphpp

```bash
cd "$HOME/molet_thirdparty/src"
git clone https://github.com/gvernard/gerlumphpp.git
cd gerlumphpp

mkdir -p maps
autoreconf -i
```

The map path below follows the folder structure adopted in this guide. Replace it with the intended location of your GERLUMPH maps.

```bash
CXXFLAGS="-g -O2 -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION" \
./configure \
  --prefix="$CONDA_PREFIX" \
  --with-map-path="$HOME/molet_thirdparty/src/gerlumphpp/maps" \
  --with-cfitsio="$CONDA_PREFIX" \
  --with-CCfits="$CONDA_PREFIX" \
  --with-png="$CONDA_PREFIX" \
  --with-fftw3="$CONDA_PREFIX" \
  --enable-gpu=no

make -j4
make install
```

### 6. Patch, build, and install vkl_lib

```bash
cd "$HOME/molet_thirdparty/src"
git clone https://github.com/gvernard/vkl_lib.git
cd vkl_lib
```

Apply the compatibility patches for CGAL linking and virtual destructors.

> Apply these patches only once to a fresh checkout. The `sed -i ''` syntax used here is specific to macOS.

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

Configure, build, and install:

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

### 7. Patch, build, and install MOLET

```bash
cd "$HOME/git_repos/molet"
```

Apply the patches for noise initialization, cleanup, virtual destructors, and CGAL linking.

> Apply these patches only once to a fresh checkout.

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

Configure, build, and install:

```bash
autoreconf -i

CXXFLAGS="-g -O2 -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION -DCGAL_DISABLE_ROUNDING_MATH_CHECK" \
./configure \
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

### 8. Configure runtime library paths

Add the Conda and MOLET library directories to the compiled executables:

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

If a runtime path is already present, `install_name_tool` may report a duplicate-path error for that entry.

### 9. Configure your shell and initialize the instrument database

Add MOLET to your shell's executable search path. Run the following append command once:

```bash
echo 'export PATH="$HOME/git_repos/molet/bin:$PATH"' >> "$HOME/.zshrc"
source "$HOME/.zshrc"
conda activate molet
```

Initialize the instrument database:

```bash
cd "$HOME/git_repos/molet"
./data/initialize_instruments.sh "$HOME/git_repos/molet"
```

### 10. Run a test

With the Conda environment active, run the provided test from the MOLET repository:

```bash
conda activate molet
cd "$HOME/git_repos/molet"

molet_driver tests/general/test_D/molet_input.json
```