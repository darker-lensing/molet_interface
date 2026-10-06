"""MOLET inputs without hand-written JSON; only Python's standard library is needed.

Keep this module beside molet_auxiliary.py, which contains internal helpers and
validation. Public configuration and execution methods remain in MoletInterface.

Setters mutate this instance and return self for optional chaining.
Mass-model setters require explicit physical parameters; other setters retain their
documented defaults. Raw JSON import and set_input preserve native fields.
Construction adds no scientific configuration. Individual setter signatures still
provide documented scalar defaults when explicitly invoked; profile dictionaries
are never filled from examples.
VKL refers to vkl_lib, the C++ library used by MOLET for mass models, light
profiles and lensing calculations; it is not a simulation input parameter.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Any

import molet_auxiliary as auxiliary


@dataclass(frozen=True)
class RunResult:
    """Artifacts and diagnostics from a MOLET run.

    Attributes
    ----------
    input_path : pathlib.Path
        Path used for the generated native JSON. The file is removed after a
        successful run unless keep_input=True; dry runs and failures retain it.
    output_dir : pathlib.Path
        Absolute path to the MOLET output directory.
    command : tuple of str
        Exact executed command, including conda run when selected.
    returncode : int or None, default=None
        Process exit code; None identifies a dry run.
    stdout : str, default=''
        Captured driver standard output; empty for a dry run.
    stderr : str, default=''
        Captured driver standard error; empty for a dry run.
    preparation_seconds : float or None, default=None
        Monotonic wall time for validation, directory setup, asset copying, and JSON
        generation. None for historical or manually constructed results.
    execution_seconds : float or None, default=None
        Monotonic wall time for subprocess launch and completion, including conda
        startup when used. Excludes plotting and preparation. None for a dry run
        or historical/manually constructed results.

    input_json : str or None, default=None
        Serialized input snapshot retained in memory for inspection and plotting
        after the temporary input file is removed. None for historical results.

    input_files_source : pathlib.Path or None, default=None
        Original auxiliary-input directory, never deleted by process(). Native
        plots can use it after the run-local input_files copy is removed.

    automatic_unity : bool, default=False
        True when process resolves omitted extrinsic variability to unity curves.
        input_json records the resolved native configuration.

    Notes
    -----
    This dataclass is frozen. Attributes cannot be reassigned.
    Driver stream logs are persisted beside input_path by process().
    """
    input_path: Path
    output_dir: Path
    command: tuple[str, ...]
    returncode: int | None = None
    stdout: str = ''
    stderr: str = ''
    preparation_seconds: float | None = None
    execution_seconds: float | None = None
    input_json: str | None = None
    input_files_source: Path | None = None
    automatic_unity: bool = False


class MoletRunError(RuntimeError):
    """MOLET execution failure, including a completed diagnostic record.

    Parameters
    ----------
    message : str
        Human-readable failure description.
    result : RunResult
        Input/output paths, command and captured process diagnostics.

    Attributes
    ----------
    result : RunResult
        Diagnostics retained for inspection after execution fails.
    """
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class MoletInterface:
    """Build and run MOLET configurations.

    molet_home: installation root; defaults to MOLET_HOME or ~/git_repos/molet.
    config: full input mapping; None starts an empty configuration.
    conda_env: None inherits the active environment; 'molet' uses conda run -n.
    conda_executable: command or absolute path, default 'conda' on PATH.
    No installation files are modified. External FITS/maps are not fabricated.
    """
    def __init__(self, molet_home=None, config=None, conda_env=None,
                 conda_executable='conda'):
        """Initialize a MOLET configuration.

        Parameters
        ----------
        molet_home : str or pathlib.Path or None, default=None
            Installation root. None uses MOLET_HOME, then ~/git_repos/molet.
        config : dict or None, default=None
            Native MOLET input. None starts with an empty dictionary. A supplied
            mapping is copied without filling missing fields. No example is loaded.
        conda_env : str or None, default=None
            Conda environment name. None inherits the current process environment.
        conda_executable : str, default='conda'
            Conda executable name or absolute path; used only when conda_env is set.

        Returns
        -------
        None
            Initializes this instance without writing files or running MOLET.
        """
        self.molet_home = Path(molet_home or os.environ.get(
            'MOLET_HOME', '~/git_repos/molet')).expanduser().resolve()
        self.conda_env = conda_env
        self.conda_executable = str(conda_executable)
        self.config = deepcopy({} if config is None else config)
        self._input_dir = None

    @classmethod
    def from_json(cls, path='molet_input.json', **kwargs):
        """Import full JSON (including // and /* */ comments), retaining every key.

        Parameters
        ----------
        path : str or pathlib.Path, default='molet_input.json'
            Input JSON file; comments are accepted. The sibling input_files directory is
            remembered.
        **kwargs : dict
            Constructor keyword arguments forwarded to MoletInterface.

        Returns
        -------
        instance : MoletInterface
            Independent configuration imported from the native JSON.

        Raises
        ------
        OSError
            Input file cannot be read.
        json.JSONDecodeError
            The file is not valid native JSON after removing comments.

        Notes
        -----
        path defaults to molet_input.json. kwargs are constructor options.
        Sibling input_files are copied into each new run directory by process().
        """
        path = Path(path).expanduser().resolve()
        instance = cls(config=auxiliary._load(path), **kwargs)
        instance._input_dir = path.parent / 'input_files'
        return instance

    def copy(self):
        """Return an independent configuration, retaining installation and asset paths.

        Returns
        -------
        instance : MoletInterface
            Deep copy of the configuration, defaults and execution/asset settings.
        """
        return deepcopy(self)

    def to_dict(self):
        """Return an independent, JSON-compatible mapping (NumPy arrays/scalars accepted).

        Returns
        -------
        config : dict
            Independent JSON-compatible configuration; NumPy scalars/arrays and Paths are converted.

        Raises
        ------
        TypeError
            A value cannot be serialized to JSON.
        ValueError
            The configuration contains NaN or infinity.
        """
        return json.loads(json.dumps(self.config, default=auxiliary._json_default, allow_nan=False))

    def to_json(self, indent=2):
        """Serialize the current configuration, including an incomplete one.

        Parameters
        ----------
        indent : int or None, default=2
            Nonnegative indentation width. None uses a single line.

        Returns
        -------
        text : str
            JSON text with a final newline, shared by print_json and write_json.

        Raises
        ------
        ValueError
            indent is invalid or the configuration contains NaN/infinity.
        TypeError
            A configuration value cannot be serialized.

        Notes
        -----
        Uses to_dict for the same NumPy/Path conversion as other exports. Does
        not require a complete runnable simulation and does not modify it.
        write_json and process additionally validate before writing files.
        """
        if indent is not None and (type(indent) is not int or indent < 0):
            raise ValueError('indent must be a nonnegative integer or None')
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False) + '\n'

    def print_json(self, indent=2):
        """Print the current JSON to stdout, including incomplete configurations.

        Parameters
        ----------
        indent : int or None, default=2
            Nonnegative indentation width. None uses a single line.

        Returns
        -------
        None
            Prints to the notebook cell or terminal without writing a file.

        Raises
        ------
        ValueError, TypeError
            JSON serialization or indentation validation fails; see to_json.

        Notes
        -----
        Uses exactly the same serialization routine as write_json. For a complete
        configuration, the default printed text matches the saved file contents.
        Use to_json instead when a string return value is needed.
        """
        print(self.to_json(indent=indent), end='')

    def set_input(self, path=(), value=None):
        """Set ANY MOLET input using a tuple of keys/list indices; () replaces root.

        Parameters
        ----------
        path : tuple of str or int, default=()
            Path through nested dictionaries/lists. String keys are literal; integer indices
            address existing list elements.
        value : JSON-compatible object, default=None
            Value to copy into the configuration. None writes JSON null. Root replacement
            requires a dictionary.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            Root replacement is not a dictionary.
        KeyError, IndexError, TypeError
            The supplied path is incompatible with the existing structure.

        Notes
        -----
        Example: set_input(('lenses', 0, 'mass_model', 0, 'pars', 'q'), 0.7).
        Missing dictionary parents are created; list indices must already exist.
        Default value=None writes JSON null; use remove() to omit an optional field.
        """
        if not path:
            if not isinstance(value, dict):
                raise ValueError('Root input must be a dictionary')
            self.config = deepcopy(value)
            return self
        node = self.config
        for key in path[:-1]:
            node = node.setdefault(key, {}) if isinstance(node, dict) else node[key]
        node[path[-1]] = deepcopy(value)
        return self

    def remove(self, path=('point_source',)):
        """Remove a section/field or list element; default disables the point source.

        Parameters
        ----------
        path : tuple of str or int, default=('point_source',)
            Path through nested dictionaries/lists. String keys are literal; integer indices
            address existing list elements.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        KeyError, IndexError, TypeError
            A parent or list element does not exist, or the path is incompatible.
        """
        node = self.config
        for key in path[:-1]:
            node = node[key]
        if isinstance(node, dict):
            node.pop(path[-1], None)
        else:
            del node[path[-1]]
        return self

    def set_cosmology(self, H0=67.7, Wm0=0.309, **extra):
        """Set H0 [km/s/Mpc] and matter density Wm0; defaults from general/test_D.

        Parameters
        ----------
        H0 : float, default=67.7
            Hubble constant in km/s/Mpc; example default from general/test_D.
        Wm0 : float, default=0.309
            Omega_m(z=0): dimensionless total matter density (baryons plus dark matter)
            relative to the present critical density; not a dark-energy equation of state.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Notes
        -----
        Changes this instance in place; reassignment is unnecessary. The local backend
        reads H0 and Wm0 and assumes flat geometry with a fixed radiation term,
        setting Omega_Lambda = 1 - Wm0 - 0.4165/H0**2. Additional fields are
        retained in JSON but do not introduce a general cosmological model.
        """
        self.config['cosmology'] = dict(H0=H0, Wm0=Wm0, **extra)
        return self

    def set_lens_redshift(self, index=0, redshift=0.77, **extra):
        """Set lens redshift (test_D default), merging extra lens-level fields.

        Parameters
        ----------
        index : int, default=0
            Zero-based lens index. An index equal to the number of lenses appends an empty lens.
        redshift : float, default=0.77
            Dimensionless redshift. The native examples use 0.77 for the lens and 2.03 for the
            source.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Notes
        -----
        index selects an existing lens; index=len(lenses) appends an empty lens.
        A new lens contains only its redshift (and explicitly supplied extra fields).
        Mass and light sections are created only by subsequent setters.
        This edits one JSON configuration, not a loop over independent runs.
        The installed point-source backend reads only lenses[0]; multiple lens
        redshifts are not a validated multi-plane implementation. For several
        deflectors on one plane, use several mass components in lenses[0].
        """
        lenses = self.config.setdefault('lenses', [])
        auxiliary._check_index(index, len(lenses), append=True)
        if index == len(lenses):
            lenses.append({})
        auxiliary._merge(lenses[index], dict(redshift=redshift, **extra))
        return self


    def set_lens_mass_model(self, model='sie', index=0, pars=None, **parameters):
        """Append a complete, explicitly parameterized mass component to a lens.

        Parameters
        ----------
        model : {'sie', 'spemd', 'external_shear', 'pert'}, default='sie'
            Native mass-model type. No physical parameter defaults are inserted.
        index : int, default=0
            Zero-based existing lens index, consistent with set_lens_redshift,
            set_lens_light_profile and set_compact_mass_model.
        pars : dict or None, default=None
            Explicit model parameters. None supplies no values. All required keys
            must be present here or in **parameters on every call.
        **parameters : dict
            Explicit named parameters; override matching keys in pars. Unknown keys
            and missing/None values raise ValueError before the configuration changes.
            SIE requires theta_E, q, pa, x0, y0. SPEMD additionally requires gam, s.
            External shear requires g, phi, x0, y0. Pert requires filepath,
            Nx, Ny, xmin, xmax, ymin, ymax, scale_factor. See Notes for units.

        Returns
        -------
        self : MoletInterface
            This same object, modified in place. Reassignment is unnecessary.

        Raises
        ------
        ValueError
            Unknown model/parameter, missing parameter, nonfinite numeric value,
            invalid grid size or extent, or empty perturbation filepath.
        TypeError
            pars is not a dictionary or None.
        IndexError
            The lens index is invalid. No mutation occurs on failure.

        Notes
        -----
        Physical interpretation of the supported models:

        * sie (Singular Isothermal Ellipsoid): an elliptical projected mass
          distribution commonly used for the total smooth mass of a galaxy lens.
          Its convergence falls as 1/R, where R is the elliptical angular radius.
          The ideal profile is singular at the center and has no finite core or
          outer truncation. In the spherical limit its three-dimensional density
          scales as r**(-2). theta_E sets the lensing strength; q, pa and x0/y0
          set the shape, orientation and center. Stellar and dark matter are not
          separated into distinct physical components by this model.
        * spemd (Softened Power-law Elliptical Mass Distribution): a smooth
          elliptical galaxy-lens model with a variable density slope and a core.
          In the local VKL implementation, kappa is proportional to
          (R**2 + s**2)**(-(gam-1)/2), with R**2 = x_rot**2 + y_rot**2/q**2.
          Thus, outside the core, projected density scales as R**(1-gam).
          gam=2 gives the isothermal slope; as s tends to zero this approaches
          the singular isothermal family. Positive s softens the central cusp.
          This is a projected mass model, not an elliptical-potential model;
          do not substitute another code's slope/normalization conventions.
        * external_shear: a spatially uniform tidal approximation to the effect
          of surrounding matter, such as neighboring galaxies or a group.
          Its potential is quadratic in position and its deflection is linear.
          It distorts images anisotropically, with shear amplitude g and angle
          phi, but contributes zero convergence (no local surface-mass density).
          It normally supplements a main deflector; it is not that deflector's
          ellipticity and does not include an external mass sheet. By itself,
          away from |g|=1, it gives an invertible linear lens mapping rather than
          the usual multiple-image galaxy lens.
        * pert: a pixelated lensing potential read from a FITS grid, rather than
          an analytic density profile or an image of source brightness. VKL
          obtains deflections from its first spatial derivatives and convergence
          from half its Laplacian. It can encode corrections to a smooth model
          or other potential structure; its physical meaning comes from the
          supplied map. It is not automatically a random perturbation or a
          microlensing map, and positivity/physical plausibility are not enforced.

        All physical parameters are required, with no implicit preset fallback:

        * sie: theta_E (Einstein-radius normalization, arcsec), q (minor/major
          axis ratio), pa (position angle, degrees), x0/y0 (center, arcsec).
        * spemd: the same five parameters, gam (dimensionless density slope;
          gam=2 is isothermal), s (core scale, arcsec). VKL uses e=(gam-1)/2.
        * external_shear: g (dimensionless shear amplitude), phi (angle, degrees),
          x0/y0 (reference center, arcsec). This has zero convergence, not a galaxy
          mass profile. A shear-only list is allowed but is not a usual strong lens.
        * pert: filepath (FITS potential grid relative to input_files), Nx/Ny
          (positive integer grid dimensions), xmin/xmax/ymin/ymax (arcsec bounds),
          scale_factor (dimensionless). The point-source branch applies this
          factor; the extended-source factory ignores it. Use 1 for consistent
          branch normalization, or pre-scale the FITS potential itself.

        VKL converts pa/phi to radians after adding 90 degrees; preserve its native
        convention when comparing other codes. The potential grid has angular
        potential units (arcsec squared). These checks are not scientific validation
        of parameter ranges or FITS contents.

        There is no macro/perturbation nesting: MOLET sums a mass_model list. SIE
        and SPEMD are typical main deflectors; shear describes an external tidal
        field. There is no interface-imposed component count limit. Every call
        appends one component, including repeated calls with the same model type.
        Existing components are never replaced by this method. The constructor
        adds none, and index selects the lens rather than a mass component.
        Other explicitly configured components remain intact; no shear is added automatically.
        Use remove(('lenses', index, 'mass_model', slot)) to remove one, or
        set_input(('lenses', index, 'mass_model'), []) to start an empty list.

        Explicit full JSON imports retain their supplied fields. The constructor is empty.
        Strict explicit parameters apply to this setter. Use set_input only when
        deliberately working with raw fields or a separately verified backend
        extension; the installed factory supports only the four types above.

        Examples
        --------
        >>> sim = MoletInterface().set_lens_redshift(redshift=0.77)
        >>> sim.set_lens_mass_model('sie', theta_E=1.2, q=0.75,
        ...                         pa=35.0, x0=0.0, y0=0.0) is sim
        True
        >>> sim.set_lens_mass_model('sie',
        ...                         theta_E=0.2, q=0.9, pa=0.0, x0=0.5, y0=0.1) is sim
        True
        """
        component = auxiliary._mass_component(model, pars, parameters)
        lenses = self.config.get('lenses', [])
        auxiliary._check_index(index, len(lenses))
        items = lenses[index].get('mass_model', [])
        items.append(component)
        lenses[index]['mass_model'] = items
        return self

    def set_source_redshift(self, redshift=2.03, **extra):
        """Set source redshift (test_D default), merging any extra source fields.

        Parameters
        ----------
        redshift : float, default=2.03
            Dimensionless redshift. The native examples use 0.77 for the lens and 2.03 for the
            source.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.
        """
        auxiliary._merge(self.config.setdefault('source', {}), dict(redshift=redshift, **extra))
        return self

    def set_source_light_profile(self, profile='gauss', instrument='testCAM-i', pars=None, **parameters):
        """Append an explicitly parameterized source light profile for one band.

        Parameters
        ----------
        profile : str, default='gauss'
            Light-profile type: 'gauss', 'sersic', or 'custom'.
            Physical profile parameters must be supplied explicitly.
        instrument : str, default='testCAM-i'
            Exact registered instrument/band name. Does not register a new instrument or rename
            other band keys.
        pars : dict or None, default=None
            Explicit parameters. None supplies no values; no example parameters are inserted.
        **parameters : dict
            Additional model parameters merged into pars, taking precedence over the pars
            mapping. Names and units follow the native MOLET model.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            The model/parameter name is unknown, required parameters are missing,
            a numeric value is nonfinite/boolean, or a checked range is invalid.
            Checks finish before any profile is appended.
        TypeError
            The removed component_index argument is supplied, or pars is invalid.

        Notes
        -----
        Composition and instrument selection
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Every call appends one component to source.light_profile[instrument].
        MOLET/VKL sums their surface brightness at each ray-traced source position:
        multiple Gaussians, multiple Sersic profiles, and mixed profiles are valid.
        These are components of one source at source.redshift, not independent
        systems or source planes. Repeating a call adds its light again.

        The instrument argument is the exact instrument-band identifier used as
        a dictionary key. It does not create an instrument entry or register a
        module. Call set_instrument(name=the_same_name, ...) separately, with an
        explicit noise choice. The order of these calls is flexible; validate()
        rejects profile bands absent from instruments. No morphology or flux is
        copied automatically between bands. A run can contain several instruments;
        each receives its own profiles, PSF/noise and band-prefixed output files.
        MOLET does not automatically coadd their images into one observation.

        Local registered identifiers (September 16, 2026): ECAM-r, LSSTg-g,
        LSSTi-i, delta-x, oddCAM-i, testCAM-i. testCAM-i is this argument's
        default, not the only possible instrument. Gaia is not registered in this
        installation or supplied among its bundled instrument-data modules.
        Instrument support is extensible through MOLET's module registration;
        arbitrary strings do not create calibrated instruments. These local test
        modules are not a complete or necessarily survey-calibrated instrument
        catalog. See set_instrument and docs/LIGHT_PROFILES.md.

        Physical models and required parameters
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        No physical profile parameter is filled from an example. Supply them in
        pars or keyword parameters; keywords take precedence. Required fields:

        * gauss: x0, y0, pa, q, r_eff, and exactly one of M_tot or i_eff.
          An elliptical Gaussian surface-brightness distribution, useful for a
          smooth compact source or a luminous clump. In rotated coordinates u,v,
          the installed VKL uses I=I0*exp(-(q**2*u**2+v**2)/(2*r_eff**2)).
          Thus r_eff is the Gaussian width along v; the width along u is r_eff/q.
          Despite the name, r_eff is not a half-light radius in this implementation.
          i_eff is I0, the central surface brightness, for this model.
        * sersic: x0, y0, pa, q, r_eff, n, and exactly one of M_tot or i_eff.
          An elliptical galaxy surface-brightness model with concentration set
          by n: n=1 gives an exponential profile and n=4 the de Vaucouleurs form.
          I=Ie*exp(-bn*((R/r_eff)**(1/n)-1)), R**2=u**2+v**2/q**2.
          VKL uses bn=1.9992*n-0.3271 rather than an exact half-light solution;
          r_eff is the nominal semi-major effective radius. i_eff is Ie, the
          surface brightness at that elliptical radius, not the central value.
        * custom: filepath, Nx, Ny, xmin, xmax, ymin, ymax.
          A FITS surface-brightness image for arbitrary source morphology, not a
          lensing-potential map. Optional M_tot rescales its integrated brightness;
          omission or M_tot=0 retains the map's existing normalization. Optional
          interp selects 'nearest', 'bilinear' or 'bicubic'. Omission uses the
          native nearest-neighbor fallback; other strings are rejected here. Outside the grid the
          profile evaluates to zero. No analytic geometry parameters are applied.

        Parameter units and conventions
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        x0, y0 : float, required for analytic profiles
            Source-plane angular center in arcsec.
        pa : float, required for analytic profiles
            Position angle in degrees. VKL adds 90 degrees before rotating;
            compare conventions explicitly when importing another code's model.
        q : float, required for analytic profiles
            Dimensionless minor-to-major axis ratio; physical ellipses use 0<q<=1.
        r_eff : float, required for analytic profiles
            Positive angular scale in arcsec; see the model-specific definitions.
        n : float, required for sersic
            Dimensionless Sersic index. The approximate bn limits the accuracy of
            extreme/small-n choices; nominal half-light conventions are approximate.
        M_tot : float, alternative to i_eff for analytic profiles
            Total component magnitude in this band. F=10**(-0.4*(M_tot-ZP)) is
            the integrated flux in electrons/s. The parser supplies ZP from the
            matching instrument entry. Fluxes, not magnitudes, add across components.
        i_eff : float, alternative to M_tot for analytic profiles
            Surface brightness in electrons/(s arcsec**2), with its reference
            location defined above. Specify exactly one brightness normalization.
        filepath : str or pathlib.Path, required for custom
            FITS map relative to input_files, in electrons/(s arcsec**2).
        Nx, Ny : int, required for custom
            Grid width and height in pixels, matching the FITS data.
        xmin, xmax, ymin, ymax : float, required for custom
            Source-plane grid boundaries in arcsec; maxima exceed minima.
        interp : str, optional for custom
            Native interpolation selector: nearest, bilinear or bicubic; omission
            uses nearest-neighbor. Unknown strings are rejected by the interface.

        Custom M_tot has a special zero sentinel in VKL: M_tot=0 does not request
        a zero-magnitude rescaling. A custom profile has no i_eff normalization.
        ZP is supplied by the instrument, not a separate source parameter.
        Native upsilon/upsilon_exp fields concern light-to-mass conversion and do
        not change source surface brightness; do not use them as flux parameters.
        The installed factory's irregular branch is unimplemented; supported
        profiles here are gauss, sersic and custom only. Validation checks finite numeric values (excluding booleans), 0<q<=1,
        positive r_eff/n/i_eff, nonnegative upsilon, positive integer Nx/Ny and
        ordered bounds. Magnitudes and coordinates may be negative. Optional
        native numeric keys ZP, upsilon and analytic upsilon_exp are accepted;
        all other unknown parameters are rejected. This is not a proof of
        scientific validity, Sersic-approximation accuracy or FITS consistency.

        Examples
        --------
        >>> sim = MoletInterface().set_source_redshift(redshift=2.03)
        >>> sim.set_source_light_profile('gauss', x0=0, y0=0, pa=0,
        ...                              q=0.8, r_eff=0.06, M_tot=25) is sim
        True
        >>> sim.set_source_light_profile('sersic', x0=0.1, y0=0, pa=30,
        ...                              q=0.7, r_eff=0.2, n=1, M_tot=24) is sim
        True
        >>> len(sim.to_dict()['source']['light_profile']['testCAM-i'])
        2

        # Add the matching instrument and other required sections before process().
        """
        if 'component_index' in parameters or (isinstance(pars, dict) and 'component_index' in pars):
            raise TypeError('component_index is not supported; each call appends a source light profile')
        item = auxiliary._component('light', profile, pars, parameters)
        source = self.config.setdefault('source', {})
        items = source.setdefault('light_profile', {}).setdefault(instrument, [])
        items.append(item)
        return self

    def set_lens_light_profile(self, profile='sersic', instrument='testCAM-i', index=0,
                       pars=None, mass_to_light=None, **parameters):
        """Append an independent lens light profile for the selected lens and band.

        Parameters
        ----------
        profile : str, default='sersic'
            Light-profile type: 'gauss', 'sersic', or 'custom'.
            Physical profile parameters must be supplied explicitly.
        instrument : str, default='testCAM-i'
            Exact registered instrument/band name. Does not register a new instrument or rename
            other band keys.
        index : int, default=0
            Zero-based index of the lens entry, not a mass-component index.
        pars : dict or None, default=None
            Explicit parameters. None supplies no values; no example parameters are inserted.
        mass_to_light : dict or None, default=None
            Mass-to-light parameters upsilon and upsilon_exp. None adds no mass-to-light
            block; an empty mapping also omits it. A nonempty mapping raises ValueError
            if this lens already has compact_mass_model, without changing any input.
            Remove the existing prescription explicitly before switching. Light
            profiles without mass_to_light may coexist with compact_mass_model.
        **parameters : dict
            Additional model parameters merged into pars, taking precedence over the pars
            mapping. Names and units follow the native MOLET model.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            The model/parameter name is unknown, required parameters are missing,
            a numeric value is nonfinite/boolean, or a checked range is invalid.
            Checks finish before any profile is appended.
        IndexError
            The selected lens is missing.
        TypeError
            A removed component_index/lens_index argument is supplied or pars is invalid.

        Notes
        -----
        No Sersic or mass-to-light values are inferred. mass_to_light supplies
        upsilon/upsilon_exp explicitly; None or {} omits the block.
        Both lens and source light use the same strict profile checks: known
        parameter names, finite non-boolean numbers, 0<q<=1, positive r_eff/n/i_eff,
        integer positive custom grid sizes and ordered grid bounds. The optional
        mass_to_light dictionary accepts only finite upsilon/upsilon_exp values,
        with upsilon nonnegative. Invalid calls leave the configuration unchanged.

        Every call appends to lenses[index].light_profile[instrument]. There is
        no automatic association with any entry of lenses[index].mass_model.
        MOLET collects and sums lens-light profiles per band, independently of
        the mass list: matching list positions do not imply a physical link.

        Analytic profiles have their own required x0/y0 center in arcsec in the
        lens/image-plane angular coordinate system. They do not inherit a SIE
        center, an average of mass centers, or a mass-weighted center. A custom
        FITS profile is located by its grid bounds and map content. Profile types,
        parameters and normalization follow set_source_light_profile, but describe
        the lens's light rather than the background source's light.

        For two SIE components at different centers in one lens entry, explicitly
        add two light profiles with those centers if you want light aligned with
        both masses. You may also deliberately choose different centers or a
        different number of light components. Updating mass coordinates later
        does not move the light profiles. index selects the containing lens entry;
        it never selects a SIE. instrument selects the registered observation band.

        mass_to_light belongs to this light profile and is used by MOLET's compact
        mass calculation; it does not attach the light profile to a particular
        SIE/SPEMD or create/update an analytic mass-model component.


        """
        if 'component_index' in parameters or (isinstance(pars, dict) and 'component_index' in pars):
            raise TypeError('component_index is not supported; each call appends a lens light profile')
        if 'lens_index' in parameters:
            raise TypeError('Use index instead of lens_index')
        auxiliary._check_index(index, len(self.config.get('lenses', [])))
        item = auxiliary._component('lens_light', profile, pars, parameters)
        if mass_to_light is not None:
            if not isinstance(mass_to_light, dict):
                raise TypeError('mass_to_light must be a dictionary or None')
            if mass_to_light and 'upsilon' not in mass_to_light:
                raise ValueError('mass_to_light requires upsilon')
            unknown = mass_to_light.keys() - {'upsilon', 'upsilon_exp'}
            if unknown:
                raise ValueError(f'Unknown mass_to_light parameters: {sorted(unknown)}')
            for key, value in mass_to_light.items():
                auxiliary._finite_light_number(key, value)
                if key == 'upsilon' and value < 0:
                    raise ValueError('upsilon must be nonnegative')
            if mass_to_light:
                auxiliary._check_compact_prescription(self.config['lenses'][index], 'mass-to-light')
                item['mass-to-light'] = deepcopy(mass_to_light)
        items = self.config['lenses'][index].setdefault('light_profile', {}).setdefault(instrument, [])
        items.append(item)
        return self


    def set_compact_mass_model(self, models=None, index=0):
        """Set the mean compact-matter distribution used to characterize microlensing.

        Parameters
        ----------
        models : list of dict or None, default=None
            Full nonempty list of profiles, each with exactly type and pars. Types
            are gauss, sersic and custom. No physical parameters are inferred.
            None and [] raise ValueError. This replaces the whole previous list;
            it does not append. Inputs are validated and copied before mutation.
        index : int, default=0
            Zero-based existing lens entry, not a mass-component index.

        Returns
        -------
        self : MoletInterface
            This same object, modified in place. Existing light profiles and
            their mass-to-light blocks are never removed automatically.

        Raises
        ------
        ValueError, TypeError
            The list, profile type, parameter names, required fields, numeric
            values or checked geometric ranges are invalid. See light-profile
            setters for the common profile checks. A mass-to-light block already
            present in this lens also raises ValueError. Failure does not change input.
        IndexError
            The selected lens does not exist.

        Notes
        -----
        Physical role
        ~~~~~~~~~~~~~
        The macro mass_model (e.g. SIE/SPEMD) determines total convergence kappa,
        deflections and multiple images. compact_mass_model separately describes
        the mean surface density in compact objects, typically stars, as
        kappa_star = Sigma_compact / Sigma_critical. MOLET evaluates it at the
        macro-image positions and computes the smooth fraction s=1-kappa_star/kappa.
        It is a decomposition of the total mass, not an extra macro deflector:
        this setter does not add deflections to mass_model or change its parameters.

        The local backend reuses light-profile shapes for this direct field,
        summing their value() outputs as dimensionless kappa_star. For an analytic
        profile prefer i_eff: it is convergence at the Sersic effective radius or
        central convergence for a Gaussian, not brightness in electrons/s/arcsec^2.
        x0/y0/r_eff are angular coordinates/scales in arcsec; q, pa and n follow
        the light-profile definitions. No center is inherited from a SIE.
        M_tot is a native alternative normalization evaluated with dummy ZP=0;
        it is not an observed-band magnitude in this direct-mass branch. A custom
        FITS grid should encode the intended dimensionless convergence field;
        omit M_tot to retain its values (zero is also the native no-rescaling
        sentinel). Grid contents and scientific normalization require user checks.

        Custom FITS compact convergence
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Use type='custom' for an externally computed, spatially resolved mean
        compact-matter distribution that need not follow a Gaussian or Sersic law.
        Supply a two-dimensional image in the primary FITS HDU. Pixel values must
        already represent dimensionless kappa_star, NOT stellar mass per pixel,
        surface mass density in physical units, light intensity or magnification.
        If starting from Sigma_compact, divide by Sigma_critical using consistent
        units and the chosen lens/source geometry before writing the FITS.
        This is a lens-plane convergence map, not a GERLUMPH magnification map.

        Required pars entries (none has an inferred default):

        - filepath: relative filename beneath the auxiliary input directory, e.g.
          'kappa_star.fits' or 'maps/kappa_star.fits'. The native parser prepends
          run/input_files/; do not supply an absolute filename here and do not
          repeat the input_files/ prefix. Supply the original asset directory
          through process(input_files=assets).
        - Nx, Ny: positive integer grid dimensions along x and y. They must match
          the FITS array; the expected NumPy arrangement is (Ny, Nx). The native
          reader does not reliably reject dimension mismatches. For non-square
          or asymmetric inputs, verify the returned map orientation explicitly.
        - xmin, xmax, ymin, ymax: angular OUTER grid boundaries in arcsec, in the
          same image-plane coordinate system as the lens model. Maxima must
          exceed minima. Pixel centers lie at xmin+(j+0.5)*(xmax-xmin)/Nx and
          ymin+(i+0.5)*(ymax-ymin)/Ny. This explicit geometry controls placement;
          an astronomical FITS WCS is not used to register the map automatically.

        Optional pars entries:

        - interp: 'nearest', 'bilinear' or 'bicubic'. Omission uses the native
          nearest-neighbor fallback. Interpolation acts on kappa_star; bicubic
          interpolation can overshoot, so check positivity when using it.
        - M_tot: omit it to preserve the FITS normalization. Zero is also the
          native no-rescaling sentinel. A nonzero value invokes the reused
          light-profile magnitude normalization with dummy ZP=0 and rescales
          the map. It is neither a stellar mass nor a physically meaningful
          observed magnitude for this direct compact-mass prescription.
        - ZP and upsilon are accepted by the shared profile schema but are not
          controls for this direct compact normalization: MOLET supplies dummy
          ZP=0 and evaluates value(), not value_to_mass(). Leave them out here.

        There are no x0/y0, q or pa parameters for a custom grid. Encode offsets,
        ellipticity and orientation in the map and its coordinate boundaries.
        Outside the supplied rectangle, the profile returns zero. Cover all image
        positions of interest; an image outside it would receive no compact
        contribution from this component. Multiple compact components in models
        are summed, and each call replaces the whole list.

        The interface checks parameter names, dimensions as integers, ordered
        bounds and supported interpolation names. It does not read this FITS to
        certify its HDU, dimensions, orientation, finite/nonnegative pixels or
        consistency with the macro model. In particular, verify physically that
        0 <= kappa_star <= kappa where relevant. Inspect lens_kappa_star_super.fits
        and k_star/s in multiple_images.json after execution. These compact-mass
        products are computed by the backend when point_source is present.

        Keep original assets outside run_dir: process empties run_dir before a
        new run, copies assets into run/input_files, and removes that copy after
        success by default. The original asset directory is preserved.

        Two exclusive prescriptions per lens
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        With point_source present, MOLET requires exactly one of:

        1. A nonempty compact_mass_model, supplied by this setter.
        2. One or more lens-light profiles with mass-to-light blocks, confined
           to ONE instrument band for that lens. Several profiles in that same
           band are allowed; using two bands counts the same stellar matter twice.

        This requirement applies even when point_source has no variability or
        microlensing settings. Without point_source, the local backend does not
        compute this compact field; neither prescription is then mandatory.
        validate(), write_json() and process() check these rules, also for raw
        imported JSON. They reject conflicting prescriptions and multiple
        mass-to-light bands even before a point source is enabled. Specified
        compact profiles and mass-to-light values must themselves be valid.

        mass_to_light must contain a nonnegative finite upsilon; optional
        upsilon_exp is finite and controls the native radial conversion. These
        factors multiply the native light profile to obtain kappa_star. A physical
        stellar M/L in solar units cannot be inserted without the appropriate
        unit conversion. For custom light maps the native conversion is constant.
        Both setters reject the second, incompatible prescription immediately,
        leaving the configuration unchanged, regardless of call order. To switch
        to light-based conversion, first remove(('lenses', index, 'compact_mass_model')).
        To switch to direct compact mass, first remove each existing conversion
        with remove(('lenses', index, 'light_profile', band, slot, 'mass-to-light')).
        Here slot is the zero-based light-profile position in that band. The light
        profiles themselves can remain. Then call the desired setter. An empty
        compact list is not a supported no-microlensing switch.

        Scope and limitations
        ~~~~~~~~~~~~~~~~~~~~~
        This setter does NOT generate individual stars, create magnification
        maps, download GERLUMPH data, or enable time variability. Map availability,
        source variability and velocities are configured separately. Physically,
        the compact contribution should be consistent with total convergence;
        the interface cannot verify kappa_star<=kappa everywhere from parameters
        alone. The local backend clips negative s to zero at image positions;
        this does not establish that the chosen decomposition is consistent.
        Multiple-lens-plane limitations of the backend still apply.

        Examples
        --------
        >>> sim = MoletInterface().set_lens_redshift(redshift=0.77)
        >>> sim.set_compact_mass_model(models=[{
        ...     'type': 'sersic',
        ...     'pars': {'x0': 0.0, 'y0': 0.0, 'q': 0.8, 'pa': 35.0,
        ...              'r_eff': 1.0, 'n': 4.0, 'i_eff': 0.1}
        ... }]) is sim
        True

        Configure an existing square FITS convergence map on your existing sim.
        The lens at index=0 must already exist, with no mass-to-light conversion.
        This example only sets input; it does not create the FITS or run MOLET:

        >>> sim.set_compact_mass_model(index=0, models=[{
        ...     'type': 'custom',
        ...     'pars': {'filepath': 'kappa_star.fits', 'Nx': 256, 'Ny': 256,
        ...              'xmin': -2.0, 'xmax': 2.0, 'ymin': -2.0, 'ymax': 2.0,
        ...              'interp': 'bilinear'}
        ... }]) is sim
        True

        For a FITS stored at assets/kappa_star.fits, after configuring the rest
        of the system call sim.process(run_dir=run_dir, input_files=assets).
        With Astropy, a precomputed array can be saved using
        fits.PrimaryHDU(kappa_star.astype('float32')).writeto(path).
        Astropy is needed only to create/read FITS in Python, not by this setter.

        The example values are illustrative, not a calibrated stellar-mass prior.
        """
        auxiliary._check_index(index, len(self.config.get('lenses', [])))
        checked = auxiliary._checked_compact_models(models)
        lens = self.config['lenses'][index]
        auxiliary._check_compact_prescription(lens, 'compact_mass_model')
        lens['compact_mass_model'] = checked
        return self

    def set_instrument(self, name='testCAM-i', ZP=22.19, xmin=-1.75, xmax=1.75,
                       ymin=-1.75, ymax=1.75, time=None, **extra):
        """Add instruments by calling this method repeatedly with different names.

        Each name configures a separate observation of the same lens/source system.
        Reusing a name updates that instrument instead of adding a duplicate.
        Configure its noise only with set_noise(..., instrument=name); an update
        here preserves noise already configured for that name.

        Parameters
        ----------
        name : str, default='testCAM-i'
            Exact instrument/band name to replace or append; it must exist in the MOLET
            instrument registry when running.
        ZP : float, default=22.19
            Photometric magnitude zero point; example default from general/test_D.
        xmin : float, default=-1.75
            Minimum horizontal field-of-view coordinate in arcsec.
        xmax : float, default=1.75
            Maximum horizontal field-of-view coordinate in arcsec.
        ymin : float, default=-1.75
            Minimum vertical field-of-view coordinate in arcsec.
        ymax : float, default=1.75
            Maximum vertical field-of-view coordinate in arcsec.
        time : array-like or None, default=None
            Nondecreasing observing epochs in days. None omits time; required for variability.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        TypeError
            noise is supplied; use set_noise instead.

        Notes
        -----
        Bounds are arcsec, ZP magnitude zero point. New instruments have no noise
        model until set_noise is called. time=None omits epochs;
        otherwise supply days in nondecreasing order. extra uses literal JSON keys.
        Instrument PSF/resolution live in MOLET's registry, not this run JSON.
        This does not rename existing light-profile bands.

        Calling with a new name appends an observation band; calling with an
        existing name replaces its observing fields (time=None removes epochs)
        while preserving its separately configured noise. The source light-profile dictionaries are unaffected. Multiple
        instruments produce separate observations of the same lens/source system,
        not a sum of instrument responses or independent lensing systems.

        Registered locally on September 16, 2026 (exact identifiers):
        ECAM-r, LSSTg-g, LSSTi-i, delta-x, oddCAM-i, testCAM-i.
        Gaia is absent from the local registry and bundled specifications.
        The list is installation-dependent and extensible, not an enum imposed
        by this setter. Each registry module supplies specs.json and psf.fits;
        resolution, PSF, readout and wavelength/throughput are module properties.
        Registering a module is separate from selecting it with this setter.
        Inspect molet_home/instrument_modules for the current local inventory.

        Source brightness is independently specified per matching band in
        source.light_profile; profiles are not copied to newly added instruments.
        The local extended-source backend loops over instruments. Some global
        diagnostics and the point-source search field use the first instrument's
        field of view, so differing footprints need care. See docs/LIGHT_PROFILES.md
        for the JSON structure, available modules and per-band composition.
        """
        if 'noise' in extra:
            raise TypeError('Use set_noise(..., instrument=name); set_instrument no longer accepts noise')
        item = {'name': name, 'ZP': ZP, 'field-of-view_xmin': xmin,
                'field-of-view_xmax': xmax, 'field-of-view_ymin': ymin,
                'field-of-view_ymax': ymax}
        if time is not None:
            item['time'] = deepcopy(time)
        item.update(deepcopy(extra))
        instruments = self.config.setdefault('instruments', [])
        index = next((i for i, cam in enumerate(instruments) if cam['name'] == name), None)
        if index is not None and 'noise' in instruments[index]:
            item['noise'] = deepcopy(instruments[index]['noise'])
        auxiliary._put(instruments, item, index)
        return self

    def set_noise(self, model='PoissonNoise', instrument='testCAM-i',
                  Msb=22.8, texp=200, sn=0.0, sigma=0.0, **extra):
        """Configure noise for one existing instrument; this is the dedicated noise setter.

        Call set_instrument first. Use instrument to select the band; repeated
        calls replace only that band's noise model. NoNoise is an explicit choice.

        Parameters
        ----------
        model : str, default='PoissonNoise'
            Noise type: 'PoissonNoise', 'UniformGaussian' or 'NoNoise'.
        instrument : str, default='testCAM-i'
            Exact registered instrument/band name. Does not register a new instrument or rename
            other band keys.
        Msb : float, default=22.8
            Sky surface brightness in mag/arcsec^2; used by PoissonNoise.
        texp : float, default=200
            Exposure time in seconds; must be positive for PoissonNoise.
        sn : float, default=0.0
            Signal-to-noise setting for UniformGaussian; backend default zero.
        sigma : float, default=0.0
            Noise standard deviation in pixel flux units for UniformGaussian; backend default
            zero.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            The noise model is unknown.
        StopIteration
            The requested instrument is absent.

        Notes
        -----
        Msb [mag/arcsec²], texp [s] default to test_D. UniformGaussian uses sn
        (signal/noise) or sigma (pixel flux units), whose backend defaults are zero;
        choose a positive value to generate noise. extra passes backend fields.
        """
        values = {'type': model}
        if model == 'PoissonNoise':
            values.update(Msb=Msb, texp=texp)
        elif model == 'UniformGaussian':
            values.update(sn=sn, sigma=sigma)
        elif model != 'NoNoise':
            raise ValueError(f'Unknown noise model: {model}')
        values.update(extra)
        cam = next(c for c in self.config['instruments'] if c['name'] == instrument)
        cam['noise'] = deepcopy(values)
        return self

    def set_output_options(self, super_factor=10, conserve_flux=False,
                           convolve_lens=False, output_PS_cutouts=False, **extra):
        """Set output options (test_D defaults).

        Parameters
        ----------
        super_factor : int, default=10
            Positive integer spatial oversampling factor. Default matches the C++ backend.
        conserve_flux : bool, default=False
            Use the backend PSF flux-conservation option.
        convolve_lens : bool, default=False
            Convolve lens light with the instrumental PSF.
        output_PS_cutouts : bool, default=False
            Produce observing-epoch point-source cutouts when the required variability inputs
            exist.
        **extra : dict
            Additional literal JSON fields; values are copied. See Notes for their destination
            and merge semantics.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Notes
        -----
        super_factor is the positive integer spatial oversampling; conserve_flux
        selects PSF flux conservation; convolve_lens convolves lens light;
        output_PS_cutouts produces epoch cutouts for variable point sources.
        extra retains additional literal MOLET options.
        """
        self.config['output_options'] = dict(super_factor=super_factor,
            conserve_flux=conserve_flux, convolve_lens=convolve_lens,
            output_PS_cutouts=output_PS_cutouts, **extra)
        return self


    def set_point_source(self, x0=-0.05, y0=0.05, M_tot_unlensed=25.0,
                         triangle_size=0.3, **extra):
        """Enable/update a macro point source, optionally coexisting with extended light.

        Parameters
        ----------
        x0 : float, default=-0.05
            Horizontal angular position in the source plane, in arcsec, in the
            coordinate system used by the lens model. This is the unlensed source
            position, not the position of any observed multiple image.
        y0 : float, default=0.05
            Vertical angular position in the source plane, in arcsec. Set x0/y0
            equal to the host light-profile center if the nucleus is centered in
            its host; the interface does not enforce or infer that alignment.
        M_tot_unlensed : float, default=25.0
            Total apparent magnitude the unresolved source would have without
            gravitational magnification, used for the static macro-only image.
            Despite the uppercase M in the native JSON name, this is NOT an
            absolute magnitude, stellar mass, luminosity, or surface brightness.
            It refers to the nucleus alone, excluding any extended host light.
            MOLET converts it using each instrument's photometric zero point:
            F_unlensed = 10**(-0.4 * (M_tot_unlensed - ZP)) electrons/second.
            Smaller magnitudes mean brighter sources. Match the magnitude's
            photometric convention to the instrument ZP; this parameter does
            not itself select AB/Vega or apply a distance-modulus correction.
            One scalar is reused in all bands for this static product; it does
            not specify an AGN spectrum or separate magnitudes per instrument.
            It does not normalize the time-dependent light curves; see Notes.
            The default 25.0 is an example value, not an inferred AGN brightness.
        triangle_size : float, default=0.3
            Positive angular scale, in arcsec, of the INITIAL numerical grid
            used to search for multiple images in the image plane. It is not
            the source radius, PSF width, detector pixel size, or microlensing
            emission-region size. The backend sets grid dimensions approximately
            to round(search_width / triangle_size) and
            round(search_height / triangle_size), then triangulates the grid.
            Thus this controls initial sampling rather than an exact bound on
            every triangle edge. Smaller values sample more densely and increase
            initial work/memory; excessively coarse sampling can miss images.
            The default 0.3 follows the supplied examples and is not a universal
            accuracy guarantee. See Notes for refinement and convergence checks.
        **extra : dict
            Additional native fields merged recursively into point_source;
            supplied values are copied. Existing variability is preserved unless
            explicitly updated. A direct light_profile/profiles field is rejected:
            use the extended-source or microlensing setters for those profiles.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            Coordinates/magnitude are not finite real numbers, triangle_size
            is not positive, a light profile is attached directly to the point
            source, or supplied variability has invalid structure/modes.

        Notes
        -----
        x0/y0/triangle_size are arcsec; M_tot_unlensed is magnitude. Existing
        variability is preserved. No variability is enabled by this call alone.
        The explicitly invoked scalar defaults follow general/test_A.

        Static brightness versus variable light curves
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Each macro image receives abs(mu) * F_unlensed, where mu is its signed
        macro magnification, before PSF rendering. For example, ZP=25 and
        M_tot_unlensed=25 give 1 electron/second before lensing; abs(mu)=10 gives
        10 electrons/second for that image (magnitude 22.5), before aperture losses
        and noise. This is the point-source contribution; static host and lens
        light are added separately. The static output includes
        OBS_<instrument>_ps_macro_noiseless.fits.

        With variability enabled, intrinsic input/generated light curves supply
        the brightness used for time-dependent products. Changing only
        M_tot_unlensed changes the static macro-only product, not those curves.
        Keep their normalizations consistent explicitly if that is your intent.

        Image finding and triangle_size
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        MOLET maps the initial image-plane triangles through the lens equation
        and selects those whose mapped triangles contain (x0, y0). It refines
        candidate regions to locate the multiple images. The search rectangle
        is derived from the instrument fields of view (also including the origin
        in this backend). triangle_size controls only the initial grid; the
        backend has a separate refinement stopping scale equal to the search
        rectangle diagonal divided by 300. It is not a requested final position
        uncertainty, and reducing it alone does not guarantee arbitrary accuracy.

        At fixed search area, halving triangle_size gives roughly four times as
        many initial grid samples; total runtime need not increase by exactly
        four because later refinement depends on the lens and source position.
        For a new system, compare image counts, positions and magnifications
        after reducing this scale, especially near caustics. The interface only
        checks that triangle_size is finite and positive; it does not certify
        image completeness or choose a converged value for you.

        Extended light, variability and compact lens matter
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Intrinsic variability (custom light curves or DRW) and extrinsic
        variability (custom curves or microlensing) are configured with the
        variability setters. Running variability requires intrinsic settings and
        observation times for every instrument. If extrinsic is omitted, process
        generates unity curves to represent no microlensing. There is no supported 'none'
        mode: a constant contribution must be represented by appropriate input
        curves, not by an invented mode name. M_tot_unlensed sets the static
        macro-only image magnitude; variable fluxes come from the light curves.

        set_source_light_profile adds separate, static extended emission, e.g.
        the host galaxy of this quasar. Both may coexist at source.redshift;
        their centers are independent. A point_source itself has no analytic
        light_profile. Its macro images are rendered with the instrument PSF.
        extrinsic.profiles instead describes finite emission-region profiles
        for microlensing, despite the macro source being treated as a point.
        It does not replace source.light_profile or vary the host galaxy.

        Every lens also needs a compact-mass prescription before execution,
        even for a static point source; see set_compact_mass_model.
        remove('point_source') disables the whole point source.

        """
        value = deepcopy(self.config.get('point_source', {}))
        auxiliary._merge(value, dict(x0=x0, y0=y0, M_tot_unlensed=M_tot_unlensed,
                           triangle_size=triangle_size, **extra))
        auxiliary._check_point_source(value)
        self.config['point_source'] = value
        return self

    def set_variability(self, intrinsic=None, extrinsic=None, unmicro=None):
        """Replace variability with only the explicitly supplied sections.

        Parameters
        ----------
        intrinsic : dict or None, default=None
            Full intrinsic configuration. None omits this section.
        extrinsic : dict or None, default=None
            Full extrinsic configuration. None omits this section.
        unmicro : dict or None, default=None
            Optional band-to-reverberation mapping. None omits it.

        Returns
        -------
        self : MoletInterface
            This instance, modified in place.

        Raises
        ------
        ValueError
            No point source exists, or supplied sections have invalid structure
            or unsupported mode names.

        Notes
        -----
        No curves, velocity parameters, profiles or point source are inferred.
        Supplied intrinsic/extrinsic branches are checked immediately for required
        fields, supported keys and numeric ranges, before any configuration change.
        Intrinsic and extrinsic branches may be added in separate calls, but
        each supplied branch must contain all parameters required by its model;
        validate/process require intrinsic configuration. An omitted extrinsic
        branch is resolved by process to unity curves (no microlensing).
        A combined replacement also rejects DRW with moving_variable_source,
        or unmicro with moving_variable_source/expanding_source, before mutation.
        Extended source light remains static. unmicro, when present, must have
        a valid lag configuration for every instrument before execution.
        """
        if 'point_source' not in self.config:
            raise ValueError('Create the point source explicitly with set_point_source first')
        value = {}
        if intrinsic is not None:
            value['intrinsic'] = deepcopy(intrinsic)
        if extrinsic is not None:
            value['extrinsic'] = deepcopy(extrinsic)
        if unmicro is not None:
            value['unmicro'] = deepcopy(unmicro)
        auxiliary._check_variability(value)
        self.config['point_source']['variability'] = value
        return self

    def set_intrinsic_variability(self, model='custom', scale_factor=1.0,
                                  N_in=None, absolute_i_mag=None, mean_mag=None, **extra):
        """Configure brightness changes originating in the unresolved source itself.

        Intrinsic variability describes, for example, fluctuations of an AGN's emission.
        The multiple macro images share this source signal; MOLET applies their lensing
        time delays and magnifications when combining the signals. This setter does not
        vary the extended host light set by set_source_light_profile.

        Parameters
        ----------
        model : str, default='custom'
            'custom' reads supplied magnitude light curves, one file per instrument.
            'drw' generates stochastic damped-random-walk magnitude light curves.
            These are the two supported intrinsic modes; there is no 'none' mode.
        scale_factor : float, default=1.0
            Dimensionless multiplicative factor applied AFTER converting intrinsic
            magnitudes into flux: F = scale_factor * 10**(-0.4 * (m - ZP)), in
            electrons/second. It scales the whole flux curve, not just its fluctuations
            and not its time axis. For positive scale_factor, the equivalent magnitude
            shift is -2.5*log10(scale_factor): 2 doubles the flux and brightens it by
            about 0.753 mag. One leaves the supplied normalization unchanged.
            Must be a positive finite real number; invalid values raise ValueError.
        N_in : int or None, default=None
            Number of stochastic intrinsic realizations for 'drw', not the number of
            epochs, macro images or sources. Supply a positive integer for DRW.
            None omits the field; selecting DRW without it raises ValueError immediately.
            For 'custom', the file's outer array determines the number of realizations;
            this parameter is not used to generate or select custom curves.
        absolute_i_mag : float or None, default=None
            Absolute i-band magnitude of the AGN used by the DRW variability calibration.
            Unlike point_source.M_tot_unlensed, this IS an absolute magnitude. It enters
            the empirical variability amplitude/timescale relations and the backend's
            inferred black-hole-mass relation; it is not the observed mean brightness.
            Required for DRW; None omits it and raises ValueError when DRW is selected. Custom curves do not use this parameter.
        mean_mag : dict[str, float] or None, default=None
            Mapping from exact instrument names to mean unlensed apparent magnitudes,
            e.g. {'testCAM-i': 25.0}. Supply every configured band for DRW. These are
            magnitudes before the scale_factor flux rescaling, with conventions matching
            each instrument ZP. None omits the mapping; no mean is inferred from
            M_tot_unlensed. IMPORTANT: the local DRW implementation currently uses only
            the shortest-wavelength band's mean and copies its curves into other bands;
            see Notes before using DRW for multiband simulations. Unused for custom.
        **extra : dict
            Additional fields copied into the intrinsic section and validated
            against the selected model. Unknown or unused keys raise ValueError;
            seed is not a supported input for this backend.

        Returns
        -------
        self : MoletInterface
            This instance, modified in place. Replaces the entire intrinsic section;
            preserves extrinsic and unmicro. Repeated calls do not append realizations.

        Raises
        ------
        ValueError
            No point source exists, the mode or a parameter name is unsupported,
            a required model parameter is missing/None, or a checked numeric
            value, range, integer count or nested structure is invalid. Checks
            occur before mutation, also for set_variability and raw configurations
            when validate() is called. Missing sibling branches and instrument-band
            dependencies are checked by validate() before execution.
        FileNotFoundError, ValueError
            process() checks required variability files before creating a run
            directory, including dry runs. Custom curves require finite,
            increasing time arrays, matching signal/error lengths, consistent
            realization counts and observation coverage. FITS existence is
            checked; FITS contents, macro-image order/count, GERLUMPH availability
            and additional lens-delay coverage still require backend checks.

        Notes
        -----
        DRW is rejected if moving_variable_source is already configured because
        the backend would overwrite its generated curves with FITS-derived ones.
        The check also applies when the extrinsic branch is set later. Failure
        leaves all existing variability unchanged.

        Custom files and units
        ~~~~~~~~~~~~~~~~~~~~~~
        Supply input_files/<instrument>_LC_intrinsic.json, where <instrument> is the
        exact configured name. Pass the asset directory to process(input_files=...),
        or use the input_files directory associated with from_json(). Each file holds
        an array of realizations; each realization has equally long time and signal
        arrays. signal contains unlensed APPARENT MAGNITUDES, not flux or magnification.
        For example, this is one realization with three samples (illustrative only):

            [{"time": [0, 10, 20], "signal": [25.0, 24.8, 25.1],
              "dsignal": [0, 0, 0]}]

        Use increasing times in days on the same time coordinate as instrument.time.
        In the local combination code, cosmological time dilation of intrinsic input
        curves is commented out: no automatic multiplication by (1+source.redshift)
        occurs. Convert rest-frame times yourself when preparing custom inputs. Curves
        must span the observations and the lens-delay padding checked by MOLET, with
        adequate sampling for interpolation; the three-point example is not a complete
        run input. Zero dsignal entries can be supplied; the ordinary combination path
        does not propagate intrinsic measurement errors into its output uncertainty.
        All bands should provide matching realization counts and a consistent pairing.

        DRW physics and local implementation limits
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        A damped random walk is a correlated stochastic process that tends back toward
        a mean magnitude on a characteristic timescale. The backend uses empirical
        MacLeod-style relations for its long-term amplitude and damping time, with
        absolute_i_mag and rest wavelength as inputs. These are model assumptions, not
        a general prescription for every active nucleus or transient.

        The installed generator selects the shortest rest wavelength using instrument
        band-edge midpoints, generates N_in curves there, and writes those SAME curves
        to the other bands. It does not yet implement the separate requested mean_mag
        values or wavelength-dependent variability in those other bands. For controlled
        multiband means/colors/variability, provide custom curves. Its random seed is
        hard-coded to 123; identical inputs do not request a fresh independent draw on
        every run. Its generated time grid uses roughly one-day observer-coordinate
        spacing, without the commented-out redshift conversion. This documentation
        reports the installed implementation rather than promising a corrected model.

        Dependencies and normalization
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Create point_source first. To run, configure instrument times and the lens
        compact-mass prescription. Omitted extrinsic is resolved automatically to
        unity curves by process. Omitted extrinsic is resolved
        automatically to unity curves by process. A constant custom extrinsic signal
        of one can represent no time-dependent microlensing; there is no 'none' mode.
        M_tot_unlensed controls the separate static macro-only image, not these curves.
        The setters only configure inputs; process() performs the actual generation/run.

        Examples
        --------
        Configure one illustrative DRW realization; other required run sections and
        assets are intentionally outside this snippet:

        >>> sim = MoletInterface().set_point_source()
        >>> sim.set_intrinsic_variability('drw', N_in=1, absolute_i_mag=-23.0,
        ...                              mean_mag={'testCAM-i': 25.0}) is sim
        True
        >>> sim.set_intrinsic_variability('custom', scale_factor=1.0) is sim
        True
        """
        if 'point_source' not in self.config:
            raise ValueError('Create the point source explicitly with set_point_source first')
        value = dict(type=model, scale_factor=scale_factor, **extra)
        for key, item in [('N_in', N_in), ('absolute_i_mag', absolute_i_mag), ('mean_mag', mean_mag)]:
            if item is not None:
                value[key] = deepcopy(item)
        candidate = auxiliary._updated_variability(
            self.config['point_source'].get('variability', {}), 'intrinsic', value)
        self.config['point_source']['variability'] = candidate
        return self

    def set_extrinsic_variability(self, model='moving_fixed_source', microlens_mass=1,
                                  Nex=100, pars=None, profiles=None, **extra):
        """Configure image-dependent microlensing modulation of the unresolved source.

        Extrinsic variability describes brightness changes caused along the line of
        sight, here by the compact matter in the lens, rather than changes in the AGN's
        own emission. Each macro image samples its own magnification pattern. A finite
        emitting region smooths that pattern even though the macro-lensing calculation
        treats the nucleus as a point. Its microlensing profile is distinct from the
        static extended host profile and from the lens compact-mass distribution.

        Parameters
        ----------
        model : str, default='moving_fixed_source'
            One of the following native modes:

            - 'custom': read precomputed dimensionless microlensing curves from files.
            - 'moving_fixed_source': move a fixed analytic emission profile across maps.
            - 'moving_fixed_source_custom': move a fixed FITS emission profile across maps.
            - 'moving_variable_source': move a time-evolving sequence of FITS profiles
              across maps; the changing shape also changes the microlensing response.
            - 'expanding_source': expand a uniform-disc profile at fixed map locations,
              as a simplified transient photosphere model.

            Here 'fixed' means fixed profile shape/size, not zero transverse motion.
            'custom' alone means custom LIGHT CURVES; the '_custom' suffix in the moving
            mode instead means a custom spatial brightness profile.
        microlens_mass : float, default=1
            Characteristic microlens mass in solar masses for generated map-based modes.
            It sets the physical Einstein-radius scale, proportional to sqrt(mass),
            used to interpret map pixels and source sizes. It is not the AGN black-hole
            mass, total lens mass, or compact surface-density normalization. Supply a
            positive finite physical value. The scalar default is an example assumption.
            For model='custom', this argument is omitted and has no effect.
        Nex : int, default=100
            Number of extrinsic realizations: tracks for moving modes or sampled map
            positions for expanding_source. Not the number of stars, macro images,
            observing epochs or independent lens systems. Supply a positive integer.
            The combiner pairs intrinsic and extrinsic realizations, so N_in intrinsic
            curves and Nex extrinsic curves can produce N_in*Nex mock realizations.
            For custom, file contents determine the count and this argument is omitted.
        pars : dict or None, default=None
            For every moving_* mode, provide sigma_pec_l, sigma_pec_s, sigma_disp, ra, dec.
            sigma_pec_l and sigma_pec_s are the lens/source peculiar-velocity dispersions
            in km/s; sigma_disp is the lens stellar velocity dispersion in km/s. They
            enter the effective transverse-velocity model, not the macro mass model.
            ra and dec are the system's sky coordinates in degrees, used for projection
            of the observer motion; they are not the local arcsec x0/y0 coordinates.
            No dictionary values are inferred. None raises ValueError for moving modes.
            expanding_source reads its expansion settings from **extra, not pars.
            For custom, supplying pars (even {}) raises ValueError instead of ignoring it.
        profiles : dict or None, default=None
            For moving_fixed_source, specify the emission-region size prescription and
            shape as detailed below. Physical radii are in 10^14 cm, not arcsec or
            detector pixels; e.g. r0=3.25 means 3.25e14 cm. None adds no profile defaults.
            The FITS and expanding modes use the per-mode fields described in Notes,
            not this analytic profile dictionary. For custom, any non-None profiles
            raises ValueError. Unknown or unused model fields are rejected.
        **extra : dict
            Additional fields merged into the extrinsic section itself. In particular,
            per-instrument FITS metadata and expanding-source parameters belong here,
            not inside pars/profiles. Use **{'testCAM-i': {...}} for names containing '-'.
            All such fields are explicit; no physical or file-layout defaults are added.

        Returns
        -------
        self : MoletInterface
            This same object. Replaces the whole extrinsic section while preserving
            intrinsic and unmicro. Repeated calls select a new configuration, not extra
            trajectories to append to the previous configuration.

        Raises
        ------
        ValueError
            No point source exists, the mode or a parameter name is unsupported,
            a required model parameter is missing/None, or a checked numeric
            value, range, integer count or nested structure is invalid. Checks
            occur before mutation, also for set_variability and raw configurations
            when validate() is called. Missing sibling branches and instrument-band
            dependencies are checked by validate() before execution.
        FileNotFoundError, ValueError
            process() checks required variability files before creating a run
            directory, including dry runs. Custom curves require finite,
            increasing time arrays, matching signal/error lengths, consistent
            realization counts and observation coverage. FITS existence is
            checked; FITS contents, macro-image order/count, GERLUMPH availability
            and additional lens-delay coverage still require backend checks.

        Notes
        -----
        When to use this setter
        ~~~~~~~~~~~~~~~~~~~~~~~
        Use it when modelling time-dependent microlensing of an unresolved
        emitter behind the lens. For an AGN with an approximately steady spatial
        emission profile, moving_fixed_source (analytic) or
        moving_fixed_source_custom (FITS) describes a track through a map.
        moving_variable_source requires an evolving spatial profile sequence;
        expanding_source describes an expanding emitting disc, for example a
        simplified transient photosphere. custom supplies precomputed modulation.
        These are modelling choices, not interchangeable descriptions of every
        source. The interface does not infer whether an object is an AGN or SN.

        This setter does not animate the extended host or the foreground lens
        light. Those profiles may coexist and remain static. It requires an
        explicitly configured point_source. For a wholly static simulation, omit
        variability. For intrinsic variability without changing microlensing,
        the native pipeline still requires an extrinsic branch: custom unity
        curves provide a constant factor. process generates them automatically
        when intrinsic is configured and extrinsic is omitted.
        A constant intrinsic custom curve can conversely isolate extrinsic changes.

        Enforced backend incompatibilities
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        moving_variable_source with intrinsic.type='drw' raises ValueError:
        the native FITS-profile stage overwrites the generated DRW intrinsic
        output files, so the requested DRW would not reach the final combination.
        Use explicit custom intrinsic curves with this mode, or use a fixed
        emission-profile mode with DRW. Custom intrinsic files are read from
        input_files and are separate from the FITS-derived output curves.

        unmicro cannot coexist with moving_variable_source or expanding_source:
        the corresponding native combination branches ignore that contribution.
        This is rejected even for a zero flux_ratio, rather than silently
        accepting an unused configuration. Remove the unmicro section explicitly
        before switching modes. DRW with a fixed-profile or custom extrinsic
        branch is supported. No blanket DRW/expanding_source prohibition is
        imposed: scientific appropriateness depends on the intended emitter.

        These conflicts raise immediately, in either setter order, without
        changing the configuration; set_variability checks combined replacements
        and validate() checks imported/raw JSON. Missing sibling branches can
        still be configured in later calls before validation/execution.

        Analytic profiles for moving_fixed_source
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        The profiles dictionary separates the size law (type) from the spatial shape
        (shape). Required common shape settings are incl (inclination, degrees; zero
        is face-on) and orient (orientation, degrees in the profile/map convention).
        They do not set the macro lens position angle or the sky position of the AGN.
        The local size-law branches are:

        - type='parametric': r0, l0 and nu define r_half(lambda)=r0*(lambda/l0)**nu.
          r0 is a half-light radius in 10^14 cm; l0 is the reference wavelength in nm;
          nu is the dimensionless size-wavelength exponent. These are physical inputs,
          not inferred from M_tot_unlensed or the intrinsic DRW parameters.
        - type='vector': rhalf supplies one half-light radius per instrument, in the
          order of the instruments array. Caveat: the installed C++ first converts all
          profiles values with JsonCpp asString(), including this array. This path is
          rejected by the interface because it may fail before reaching the vector-specific branch.
        - type='ss_disc': mbh, fedd and eta enter the library's thin-disc size law.
          fedd is an Eddington ratio and eta a radiative efficiency. The local formula is
          r_half=0.0097*(lambda_nm**4 * mbh**2 * fedd/eta)**(1/3), in 10^14 cm.
          The inspected source does not document mbh's mass-unit convention; verify it
          against the library before supplying a physical black-hole mass. It must not
          be assumed interchangeable with microlens_mass in solar masses.

        The active parametric/ss_disc call averages the size law over the instrument's
        wavelength/throughput arrays. Although a rest wavelength is computed elsewhere,
        this call receives the original instrument wavelengths without dividing by
        (1+z). Do not assume automatic rest-frame conversion of l0 or the size law.

        The installed profile factory recognizes shape='uniform' (uniform disc),
        'gaussian', 'exponential', 'gaussian_hole' (also requires Rin, an inner radius
        in 10^14 cm), 'thermal_hole', 'wavy' (also requires amplitude a), and 'custom'
        (requires filename and profPixSizePhys, physical pixel scale in 10^14 cm).
        For wavy the half-radius factory fixes three nodes. These factory branches are
        not all tested end-to-end by the interface. For a fixed FITS profile, prefer
        moving_fixed_source_custom, which supplies the native file paths explicitly.

        FITS profiles and expanding sources
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        moving_fixed_source_custom reads input_files/cs_<instrument>.fits and a
        per-instrument extrinsic dictionary with pixSize in 10^14 cm per pixel. FITS
        profiles carry the spatial brightness distribution; this branch sets its own
        inclination/orientation to zero. The spatial normalization is handled by the
        profile library; the intrinsic curve supplies the variable source flux.

        moving_variable_source requires per-instrument pixSize (same units), Nx/Ny
        (integer image dimensions) and time (snapshot epochs in days), with matching
        files input_files/vs_<instrument>/0000.fits, 0001.fits, etc. Its backend also
        integrates the snapshots into intrinsic magnitude curves; a separately supplied
        intrinsic curve is not necessarily the final normalization in this mode. It
        can require many map convolutions. Snapshot times, spatial units, dimensions
        and brightness calibration must be consistent with the native backend inputs.

        expanding_source reads top-level extrinsic incl/orient (degrees), v_expand
        (a mapping from each instrument name to speed in units of 10^5 km/s),
        fractional_increase (dimensionless fractional area-growth sampling control)
        and size_cutoff (positive maximum size scale, at most 7 Einstein radii). For example,
        v_expand=0.1 for a band represents 10,000 km/s, not 0.1 km/s. These fields are
        passed through **extra. Backend preflight constructs the radius/time sampling
        and estimates convolution cost; process(confirm_convolutions=...) controls
        its confirmation prompt. This is a separate expanding-disc prescription,
        not an analytic host profile with a time-dependent r_eff.

        Custom curve format and normalization
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Supply input_files/<instrument>_LC_extrinsic.json for every band. Its outer
        array follows macro-image order in output/multiple_images.json; each image
        contains an array of realizations; each realization has time, signal, dsignal
        arrays. time is in observer-coordinate days. signal is a dimensionless
        microlensing factor, NOT magnitudes or flux; dsignal is its uncertainty in the
        same units. The macro magnification is applied separately. A schematic file
        for TWO macro images and one realization per image is:

            [[{"time": [0, 10], "signal": [1, 1], "dsignal": [0, 0]}],
             [{"time": [0, 10], "signal": [1, 1], "dsignal": [0, 0]}]]

        This gives constant unity microlensing over the supplied interval; extend the
        sampling/coverage to satisfy the real observations and time delays. The image
        count/order and curve counts must match the simulation. Individual images may
        have empty arrays in native files, but the local combiner assumes at least one
        nonempty image; an all-empty file is not a safe way to disable microlensing.
        Use unity curves for a controlled constant contribution. No GERLUMPH map files
        are required by the custom-curve branch.

        Dependencies, cost and reproducibility
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Generated modes require local GERLUMPH maps matched to the macro-image lensing
        properties. This setter neither downloads maps nor generates individual stars.
        set_compact_mass_model describes compact matter IN THE LENS; profiles here
        instead describes emitting matter IN THE SOURCE. Both are needed for different
        parts of a generated microlensing calculation. Light-based compact-mass
        conversion is the alternative to set_compact_mass_model, not to profiles.

        The host light remains static. Create point_source, an intrinsic branch and
        instrument times as well; no complete simulation is implied by this setter
        alone. More tracks/positions, larger profiles or more evolving snapshots can
        increase time/storage significantly. Native generators use fixed seeds at
        several stages; repeated calls/runs are not a guarantee of independent random
        samples, and arbitrary seed keywords are not an implemented seed-control API.

        Examples
        --------
        Illustrative fixed Gaussian emission region; these values are explicit example
        assumptions, not a calibrated AGN prescription. Full lens/instrument settings
        and local maps are still required before process():

        >>> sim = MoletInterface().set_point_source()
        >>> sim.set_extrinsic_variability('moving_fixed_source', Nex=10,
        ...     microlens_mass=1.0,
        ...     pars={'sigma_pec_l': 250, 'sigma_pec_s': 250, 'sigma_disp': 250,
        ...           'ra': 340.2, 'dec': 0.0},
        ...     profiles={'type': 'parametric', 'r0': 3.25, 'l0': 102.68,
        ...               'nu': 1.33, 'shape': 'gaussian', 'incl': 0, 'orient': 0}) is sim
        True
        >>> sim.set_extrinsic_variability('custom') is sim
        True
        """
        if 'point_source' not in self.config:
            raise ValueError('Create the point source explicitly with set_point_source first')
        if model == 'custom' and (pars is not None or profiles is not None):
            raise ValueError('custom extrinsic reads light curves; pars/profiles would be ignored')
        value = {'type': model}
        if model != 'custom':
            value.update(microlens_mass=microlens_mass, Nex=Nex)
            if pars is not None:
                value['pars'] = deepcopy(pars)
            if profiles is not None:
                value['profiles'] = deepcopy(profiles)
        auxiliary._merge(value, extra)
        candidate = auxiliary._updated_variability(
            self.config['point_source'].get('variability', {}), 'extrinsic', value)
        self.config['point_source']['variability'] = candidate
        return self

    def set_unmicrolensed_variability(self, instrument='testCAM-i', model='top-hat',
                                     flux_ratio=0.3, radius=3000, t_peak=25, **extra):
        """Add delayed, non-microlensed emission driven by the intrinsic source light.

        Use this optional component for reverberating emission from a region assumed
        large enough that stellar microlensing is negligible, e.g. an idealized extended
        reprocessing region around an AGN. It remains subject to macro magnification and
        macro-image time delays. It is neither a static host profile nor a switch that
        disables microlensing for the entire source.

        Parameters
        ----------
        instrument : str, default='testCAM-i'
            Nonempty exact instrument/band name. Repeated calls with different names add
            band-specific responses; a repeated name replaces that band's entire response.
            Registration may occur later, but validate() requires exactly one response
            for every configured instrument when unmicro is present, with no extra bands.
        model : str, default='top-hat'
            Temporal response kernel: 'top-hat' spreads the response uniformly over a
            range of delays; 'delta' selects a single sampled delay. This is not a
            spatial light-profile or a stochastic variability model.
        flux_ratio : float, default=0.3
            Finite dimensionless mixing weight f in [0, 1]. The backend combines
            (1-f)*mu_micro(t)*F_intrinsic(t) + f*F_echo(t), then applies the macro
            magnification and image delays. Thus 0.3 assigns weight 0.3 to the echo and
            0.7 to the directly microlensed signal. It is NOT F_echo/F_direct, and need
            not equal the instantaneous observed echo fraction after microlensing.
            Zero removes the echo's contribution; one leaves only the echo contribution.
            Even zero still configures the kernel and requires valid parameters/files.
        radius : float, default=3000
            Positive finite response-radius parameter for top-hat, in 10^14 cm.
            The native kernel has uniform sampled weights for delays 0 <= lag < R/c,
            with c=25.9 in these units per day. Hence radius=3000 means 3e17 cm and
            a response width about 115.8 days; the continuous idealization's mean delay
            is R/(2c), about 57.9 days, not 115.8 days. This is the implemented temporal
            prescription, not a general transfer function for every emitting geometry.
            It does not set the host radius or the microlensing profile size.
            Ignored for delta; only the parameter selected by model is written/checked.
        t_peak : float, default=25
            Nonnegative finite delay interval in days for delta, not an absolute epoch
            and not a light-curve peak time. Zero selects the first kernel bin. For a
            positive value the backend selects the last sampled lag STRICTLY below
            t_peak: with daily samples, t_peak=25 selects 24 days. No fractional-bin
            interpolation is performed. Ignored for top-hat.
        **extra : dict
            Reserved for compatibility of the method signature. No additional fields
            are supported by these two kernels; supplying any raises ValueError rather
            than silently overriding type/pars or accepting ignored parameters.

        Returns
        -------
        self : MoletInterface
            This same object. Preserves intrinsic/extrinsic settings and responses in
            other bands. Checks the candidate configuration before committing it.

        Raises
        ------
        ValueError
            No point source exists; the band name, model, selected parameter, flux_ratio
            or extra fields are invalid; or the extrinsic mode is moving_variable_source
            or expanding_source, whose backend branches ignore this component.
            Failures leave the configuration unchanged. The same schema and compatibility
            checks apply to set_variability and imported/raw JSON through validate().
            validate() also rejects missing intrinsic settings or band mismatches;
        an omitted extrinsic branch is automatically resolved by process.
            Before execution, process() rejects custom intrinsic curves with nonuniform
            sampling or a duration shorter than the requested kernel support.

        Notes
        -----
        Difference from the other variability setters
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        set_intrinsic_variability supplies the driving AGN flux F_intrinsic(t), either
        from custom magnitude curves or DRW. set_extrinsic_variability supplies the
        image-dependent microlensing factor mu_micro(t). This setter derives F_echo(t)
        by convolving that SAME intrinsic flux with a normalized temporal kernel; it
        adds no independently drawn fluctuations and reads no separate unmicro input
        light-curve file. It operates on flux after intrinsic magnitude conversion and
        scale_factor, not on magnitudes. The backend writes derived curves to
        output/<instrument>_LC_unmicro.json.

        For a steady intrinsic signal the normalized response is also steady. To model
        only intrinsic variability without microlensing, use unity custom extrinsic
        curves (automatically generated by process when extrinsic is omitted);
        enabling unmicro is not required. To include static extended host light,
        use set_source_light_profile instead. This component is still rendered as part
        of the unresolved macro-source signal; it does not create a resolved nebula.

        Supported combinations and bands
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        A point source is required. The intrinsic branch can be custom or DRW. Supported
        extrinsic modes are custom, moving_fixed_source and moving_fixed_source_custom.
        Incompatible modes are rejected in either setter order, even for flux_ratio=0.
        If only some bands should contribute an echo, configure the others explicitly
        with flux_ratio=0 and a valid kernel, e.g. delta with t_peak=0. To disable this
        feature altogether, remove(('point_source', 'variability', 'unmicro')).

        Time sampling and native limitations
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        The kernel is evaluated on the intrinsic curve's time grid, not instrument.time.
        No extra (1+redshift) factor is applied to radius/c or t_peak in this code path:
        use the time convention of your intrinsic input/observations. The native routine
        uses an index-based CIRCULAR convolution: samples before the start wrap to the
        end of the curve. It also assigns equal weights per sample for top-hat. Therefore
        custom intrinsic curves must be uniformly sampled; observation times may remain
        irregular. process() checks uniformity (relative tolerance 1e-7) and requires the
        intrinsic duration to cover the requested kernel width/delay. These checks do
        not eliminate wrap-around artifacts or ensure a sufficiently resolved response.
        Provide a sufficiently long, finely sampled driving curve and inspect boundary
        regions; DRW output is sampled on the backend's roughly daily grid. The actual
        DRW extent depends on calculated lens delays and is not certified here.

        The native combined-signal uncertainty formula does not include the (1-f)
        weight used for the microlensed flux, and ignores intrinsic/echo uncertainties.
        In particular, f=1 should not be assumed to produce zero reported uncertainty.
        This setter does not correct the backend error propagation. The convolution
        also uses a quadratic loop in the intrinsic sample count, so long curves can
        increase runtime even when f=0. Defaults follow the upstream quasar/test_C
        example and are not inferred physical properties of your AGN.

        Examples
        --------
        Illustrative band response only; the rest of the simulation must be configured
        before execution. Replace the same band's top-hat response with a delta response:

        >>> sim = MoletInterface().set_point_source()
        >>> sim.set_unmicrolensed_variability('testCAM-i', model='top-hat',
        ...                                  flux_ratio=0.3, radius=3000) is sim
        True
        >>> sim.set_unmicrolensed_variability('testCAM-i', model='delta',
        ...                                  flux_ratio=0.2, t_peak=25) is sim
        True
        """
        if not isinstance(instrument, str) or not instrument.strip():
            raise ValueError('unmicro instrument must be a nonempty string')
        if extra:
            raise ValueError(f'Unsupported unmicro parameters: {sorted(extra)}')
        if model not in ('top-hat', 'delta'):
            raise ValueError('Lag model must be top-hat or delta')
        if 'point_source' not in self.config:
            raise ValueError('Create the point source explicitly with set_point_source first')
        value = dict(type=model, flux_ratio=flux_ratio,
                     pars={'radius': radius} if model == 'top-hat' else {'t_peak': t_peak})
        auxiliary._merge(value, extra)
        candidate = auxiliary._updated_variability(
            self.config['point_source'].get('variability', {}), 'unmicro', {instrument: value})
        self.config['point_source']['variability'] = candidate
        return self

    def validate(self):
        """Check JSON, basic geometry, bands and variability times before launching.

        Returns
        -------
        self : MoletInterface
            This instance, allowing chained method calls.

        Raises
        ------
        ValueError
            A checked structure, geometry, time, DRW or JSON-finiteness constraint fails.
        KeyError, TypeError
            Required nested fields are missing or have incompatible types.

        Notes
        -----
        Raises ValueError for detected mistakes. This is not a complete physical
        validator; MOLET's own initialization performs registry/PSF/asset checks.
        With point_source, each lens requires a compact-mass prescription even
        without variability. Direct compact mass and light-based conversion are
        exclusive; mass-to-light may appear in only one band per lens.
        """
        auxiliary._validate_config(self.to_dict())
        return self

    def write_json(self, path='molet_input.json'):
        """Validate and write readable JSON; default path is ./molet_input.json.

        Parameters
        ----------
        path : str or pathlib.Path, default='molet_input.json'
            Destination JSON file. Existing files are overwritten; parent directories are
            created.

        Returns
        -------
        path : pathlib.Path
            Absolute path of the written JSON file.

        Raises
        ------
        ValueError, KeyError, TypeError
            Configuration validation fails.
        OSError
            The destination cannot be created or written.

        Notes
        -----
        Serializes through to_json(), also used by print_json(), after validation.
        This explicit export overwrites the named file. process() instead requires
        a fresh run directory to protect previous samples and outputs.
        """
        self.validate()
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())
        return path

    def process(self, run_dir=None, dry_run=False, timeout=None, input_files=None,
                env=None, confirm_convolutions=True, keep_input=False,
                keep_input_files=False):
        """Write JSON and execute molet_driver, returning RunResult.

        Parameters
        ----------
        run_dir : str or pathlib.Path or None, default=None
            Directory receiving this run directly. If it already exists, ALL its
            contents are deleted before preparing the new run, including old
            products, mock directories, logs and unrelated files. No run_*
            subdirectory is created. Choose a different path to retain a previous
            run. This also applies to dry_run=True. None creates a unique system
            temporary directory. Use a dedicated output directory, not an input,
            installation or working directory.
        dry_run : bool, default=False
            Only prepare the validated JSON and copied assets when True; do not execute MOLET.
        timeout : float or None, default=None
            Maximum execution time in seconds. None sets no time limit. A timeout kills the
            process group.
        input_files : str or pathlib.Path or None, default=None
            Directory of auxiliary inputs to copy. None uses from_json() sibling input_files, or
            creates an empty input directory.
        env : dict of str to str or None, default=None
            Environment overrides merged into inherited variables. None adds no overrides.
        confirm_convolutions : bool, default=True
            Send y to the upstream convolution-cost prompt when True; send n when False.
        keep_input : bool, default=False
            Retain molet_input.json after a successful execution when True. With
            False, delete only this generated input file once MOLET has finished
            successfully. Dry runs and failures always retain it for inspection.
            The input snapshot remains available as result.input_json.
        keep_input_files : bool, default=False
            Keep the run-local input_files directory after success when True.
            Otherwise delete it after MOLET completes. Original input files are
            never deleted. Failures and dry runs always retain the local copy.
            Keep it for a self-contained archive or later file-based reprocessing.

        Returns
        -------
        result : RunResult
            Input and output paths, command, return code, stdout and stderr. Dry runs have returncode=None and empty streams.

        Raises
        ------
        MoletRunError
            MOLET reports failure, lacks its completion message, returns a nonzero code or exceeds timeout. Inspect exception.result and the persisted logs.
        NotADirectoryError
            run_dir names an existing file rather than a directory.
        FileNotFoundError
            The driver, conda executable or explicitly selected asset directory is missing.
        ValueError, KeyError, TypeError
            Configuration validation fails or a run/installation path contains whitespace.
        OSError
            File preparation or process launch fails; a partially prepared directory may remain.

        Notes
        -----
        If intrinsic variability exists and extrinsic is OMITTED, process assumes
        no microlensing: a run-local copy of the driver generates constant unity
        extrinsic curves after MOLET computes the actual macro images. One curve
        per image/band preserves macro magnifications, delays and every intrinsic
        realization. No GERLUMPH maps are needed for this branch. The original
        configuration and installed driver are unchanged; result.automatic_unity
        and stdout report the resolution, and result.input_json includes custom
        extrinsic mode. This is appropriate for intrinsic-only variability, also
        with an optional unmicro response. It does not model microlensing.
        Explicit custom/microlensing branches are never replaced; invalid explicit
        branches still raise. An empty variability section or unmicro alone is
        not sufficient: configure intrinsic first. Static inputs are unchanged.
        Existing extrinsic input files are not used by automatic unity mode.
        The native compact-mass prescription is still required for point_source.
        Custom intrinsic curves must cover the actual lens-shifted observing
        window; the run-local hook checks this before combination. Dry runs retain
        a prepared driver and placeholder arrays: actual curves require execution
        of the image-finding stage, and running the prepared adapted driver fills
        them. Automatic mode requires the verified local driver layout; an unknown
        driver fails before replacing an existing run directory.

        Files created inside run_dir:
        - molet_input.json: serialized input, removed on success by default.
        - input_files/: copied auxiliary assets, removed after success by default.
        - output/: native scientific products, diagnostics and backend log.txt.
        - driver.stdout.log / driver.stderr.log: driver streams for executed runs.
        - mock_*/: native variability realizations when enabled (siblings of output).

        Scientific products depend on the configuration and can include FITS images,
        JSON lensing diagnostics and light curves. The generated input JSON
        and run-local input_files directory are cleaned up automatically; outputs
        and logs remain. Set keep_input_files=True to retain auxiliary copies. The
        configuration object and any original file loaded by from_json are untouched.
        result.output_dir points to output/; its parent is the whole run directory.
        result.input_path records the input location even after that file is deleted.
        Use result.input_json for the exact saved input snapshot, or keep_input=True
        for file-based downstream tools and reuse across Python sessions.
        A temporary run directory is not automatically removed by the interface,
        but the operating system may clean it later; choose an explicit run_dir
        for outputs you want to retain.

        run_dir=None creates a unique directory in the system temporary folder;
        an existing explicit directory is emptied and reused at the same path.
        Old RunResult paths then refer to the latest files at that location.
        Do not run concurrent executions against the same directory.
        dry_run=True only prepares files and retains all prepared inputs.
        input_files: optional directory copied to run/input_files, default uses
        from_json()'s sibling directory, otherwise empty. env overlays inherited
        environment variables. timeout=None means no limit (seconds otherwise).
        conda_env from the constructor selects explicit conda execution. The
        inherited active environment is used otherwise; no shell activation runs.
        Required variability files and custom curve structure are checked before
        creating the run directory, including for dry_run=True. FITS existence is
        checked, not FITS pixel contents. Extra lens-delay coverage, macro-image
        ordering/count and map availability remain backend-dependent checks.
        Streams go to driver.stdout.log/driver.stderr.log. Failed stages, missing
        completion or timeout raise MoletRunError (including the result).
        Paths containing whitespace are rejected due to upstream shell quoting.
        confirm_convolutions=True answers y to the upstream expansion/movie
        computation prompt; False answers n. No terminal interaction is required.
        RunResult records monotonic preparation and execution wall times in seconds;
        neither includes plotting. execution_seconds includes conda startup when used.
        """
        if type(keep_input) is not bool:
            raise ValueError('keep_input must be a boolean')
        if type(keep_input_files) is not bool:
            raise ValueError('keep_input_files must be a boolean')
        preparation_start = time.perf_counter()
        self.validate()
        asset_dir = Path(input_files).expanduser().resolve() if input_files is not None else self._input_dir
        config, automatic_unity = auxiliary._execution_config(self.to_dict())
        auxiliary._check_variability_assets(config, asset_dir, automatic_unity)
        driver = self.molet_home / 'bin' / 'molet_driver'
        if not driver.is_file():
            driver = self.molet_home / 'molet_driver'
        if not dry_run and not driver.is_file():
            raise FileNotFoundError(f'MOLET driver not found in {self.molet_home}')
        unity_driver = auxiliary._unity_driver_text(driver) if automatic_unity else None
        if any(ch.isspace() for ch in str(self.molet_home)):
            raise ValueError('MOLET driver does not support whitespace in installation paths')
        if input_files is not None and not asset_dir.is_dir():
            raise FileNotFoundError(asset_dir)
        if run_dir is not None and asset_dir is not None:
            target = Path(run_dir).expanduser().resolve()
            if asset_dir in target.parents:
                raise ValueError('run_dir must not be inside input_files; recursive copying would result')
        run = auxiliary._create_run_directory(run_dir, protected=(self.molet_home, asset_dir))
        if asset_dir is not None and asset_dir.is_dir():
            shutil.copytree(asset_dir, run / 'input_files')
        elif input_files is not None:
            raise FileNotFoundError(asset_dir)
        else:
            (run / 'input_files').mkdir()
        (run / 'output').mkdir()
        input_path = run / 'molet_input.json'
        input_json = json.dumps(config, indent=2, allow_nan=False) + '\n'
        input_path.write_text(input_json)
        if automatic_unity:
            driver = auxiliary._prepare_unity_driver(unity_driver, run, config)
        command = [str(driver), str(input_path)]
        if self.conda_env:
            command = [self.conda_executable, 'run', '--no-capture-output', '-n',
                       self.conda_env, *command]
        preparation_seconds = time.perf_counter() - preparation_start
        result = RunResult(input_path, run / 'output', tuple(command),
                           preparation_seconds=preparation_seconds, input_json=input_json,
                           input_files_source=asset_dir, automatic_unity=automatic_unity)
        if dry_run:
            return result
        stdout_path, stderr_path = run / 'driver.stdout.log', run / 'driver.stderr.log'
        timed_out = False
        execution_start = time.perf_counter()
        with stdout_path.open('w') as out, stderr_path.open('w') as err:
            with subprocess.Popen(command, cwd=run, env={**os.environ, **(env or {})},
                                  stdout=out, stderr=err, stdin=subprocess.PIPE,
                                  start_new_session=True) as proc:
                try:
                    proc.communicate(input=b'y\n' if confirm_convolutions else b'n\n', timeout=timeout)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGKILL)
                    code = proc.wait()
        execution_seconds = time.perf_counter() - execution_start
        stdout, stderr = stdout_path.read_text(errors='replace'), stderr_path.read_text(errors='replace')
        result = RunResult(input_path, run / 'output', tuple(command), code, stdout, stderr,
                           preparation_seconds, execution_seconds, input_json, asset_dir, automatic_unity)
        if timed_out or code != 0 or 'Failed' in stdout or 'Completed successfully!' not in stdout:
            reason = 'timed out' if timed_out else 'failed'
            raise MoletRunError(f'MOLET {reason}; inspect {stdout_path} and {stderr_path}', result)
        if not keep_input:
            input_path.unlink()
        if not keep_input_files:
            shutil.rmtree(run / 'input_files')
        return result
