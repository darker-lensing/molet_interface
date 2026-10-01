# Maintaining the documentation

## Publishing

The site is published at [darker-lensing.github.io/molet_interface](https://darker-lensing.github.io/molet_interface/).

For the initial setup, a repository administrator must select **Settings → Pages → Build and deployment → Source → GitHub Actions**. Commit and push the documentation files to `main`, then check the **Documentation** workflow in the Actions tab. If the files were pushed before enabling Pages, rerun the workflow or use **Run workflow** on `main`.

Pushes to `main` build and deploy the site automatically. Pull requests build the documentation without publishing it. The workflow can also be run manually; deployment is restricted to `main`. No personal access token is required: deployment uses GitHub's workflow token and Pages environment.

## Updating content

- Edit `README.md` to update the installation page. A build hook reads it directly; there is no second copy to maintain.
- Edit Python docstrings to update the API reference automatically.
- Edit the other Markdown files in `docs/` for examples and documentation maintenance.

Keep README links absolute when linking to repository files, so they also work on the generated site.

## Local preview

From the repository root, install the documentation tools and start the preview:

```bash
python -m pip install -r requirements-docs.txt
python -m mkdocs serve
```

Open the local address printed by MkDocs. The preview watches the README and Python sources as well as the Markdown pages.

To run the same strict build used by CI:

```bash
python -m mkdocs build --strict
```

The generated `site/` directory is ignored by Git. Documentation generation reads the Python source without requiring the MOLET C++ installation, map data, or notebook execution.
