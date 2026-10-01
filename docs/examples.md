# Single-system notebook

The [Single_systems.ipynb notebook](https://github.com/darker-lensing/molet_interface/blob/main/examples/Single_systems.ipynb) configures a lens, extended source and variable point source, runs MOLET, and plots the resulting images and light curves.

After completing the installation, launch Jupyter from the repository:

```bash
conda activate molet
jupyter notebook examples/Single_systems.ipynb
```

Use the kernel from the `molet` environment. The notebook imports the installed package and saves each execution to a fresh directory under `runs/` in its working directory. These result directories are ignored by Git.

The example uses intrinsic DRW variability with unity extrinsic curves, so it requires no GERLUMPH maps and does not simulate microlensing. Its physical parameters are illustrative.

The documentation build links to the notebook; it does not execute simulations or publish generated results.
