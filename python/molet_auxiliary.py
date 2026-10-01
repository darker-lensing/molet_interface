"""Internal MOLET helpers for serialization, components and configuration checks.

Imported by molet_interface; callers should use the public MoletInterface API.
This module does not import the interface and has no external dependencies.
Validation helpers accept explicit data rather than a MoletInterface instance.
"""
from copy import deepcopy
import json
import math
from numbers import Real, Integral
from pathlib import Path
import re

def _load(path):
    """Read native JSON while preserving strings and removing C/C++ comments."""
    # Preserve strings (including URLs and escaped quotes) while removing comments.
    text = Path(path).read_text()
    pattern = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/'
    text = re.sub(pattern, lambda m: m[0] if m[0].startswith('"') else ' ', text)
    return json.loads(text)


def _merge(base, updates):
    """Recursively merge copied updates into a dictionary, returning that dictionary."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def _json_default(value):
    """Convert paths and array/scalar-like values for JSON serialization."""
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, 'tolist'):
        return value.tolist()
    if hasattr(value, 'item'):
        return value.item()
    raise TypeError(f'Not JSON serializable: {type(value).__name__}')


def _finite_light_number(name, value):
    """Reject non-real, boolean or nonfinite numerical input."""
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite real number (not a boolean)')


def _component(kind, model, pars, extra):
    """Validate and copy a complete analytic or pixelated light component."""
    if pars is not None and not isinstance(pars, dict):
        raise TypeError('pars must be a dictionary or None')
    supplied = deepcopy(dict(pars or {}, **extra))
    required = {
        'gauss': {'x0', 'y0', 'pa', 'q', 'r_eff'},
        'sersic': {'x0', 'y0', 'pa', 'q', 'r_eff', 'n'},
        'custom': {'filepath', 'Nx', 'Ny', 'xmin', 'xmax', 'ymin', 'ymax'},
    }
    if not isinstance(model, str) or model not in required:
        raise ValueError(f'Unknown light profile {model!r}')
    optional = ({'M_tot', 'interp', 'upsilon', 'ZP'} if model == 'custom'
                else {'M_tot', 'i_eff', 'upsilon', 'upsilon_exp', 'ZP'})
    missing = required[model] - supplied.keys()
    unknown = supplied.keys() - required[model] - optional
    if missing or unknown:
        raise ValueError(f'{model}: missing light parameters {sorted(missing)}; '
                         f'unknown light parameters {sorted(unknown)}')
    if model in ('gauss', 'sersic') and sum(k in supplied for k in ('M_tot', 'i_eff')) != 1:
        raise ValueError('Supply exactly one of M_tot or i_eff')
    for key, value in supplied.items():
        if key == 'filepath':
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError('filepath must be a nonempty string or Path')
            supplied[key] = str(value)
        elif key == 'interp':
            if not isinstance(value, str) or value not in ('nearest', 'bilinear', 'bicubic'):
                raise ValueError('interp must be nearest, bilinear or bicubic')
        else:
            _finite_light_number(key, value)
    if model == 'custom':
        for key in ('Nx', 'Ny'):
            if not isinstance(supplied[key], Integral) or supplied[key] < 1:
                raise ValueError(f'{key} must be a positive integer')
            supplied[key] = int(supplied[key])
        for axis in ('x', 'y'):
            if supplied[axis + 'min'] >= supplied[axis + 'max']:
                raise ValueError('Grid minima must be smaller than maxima')
    else:
        if not 0 < supplied['q'] <= 1:
            raise ValueError('q must satisfy 0 < q <= 1')
        if supplied['r_eff'] <= 0:
            raise ValueError('r_eff must be positive')
        if model == 'sersic' and supplied['n'] <= 0:
            raise ValueError('n must be positive')
        if 'i_eff' in supplied and supplied['i_eff'] <= 0:
            raise ValueError('i_eff must be positive')
    if 'upsilon' in supplied and supplied['upsilon'] < 0:
        raise ValueError('upsilon must be nonnegative')
    return {'type': model, 'pars': supplied}


def _put(items, component, index):
    """Append an item or replace an existing list entry."""
    if index is None or index == len(items):
        items.append(component)
    else:
        items[index] = component


def _check_index(index, length, append=False):
    """Validate a lens/list index, optionally allowing an append position."""
    upper = length if append else length - 1
    if type(index) is not int or not 0 <= index <= upper:
        raise IndexError(f'index must be an integer between 0 and {upper}')


def _checked_compact_models(models):
    """Validate and copy the full direct compact-mass profile list."""
    if not isinstance(models, list) or not models:
        raise ValueError('compact_mass_model must be a nonempty list of explicit profiles')
    checked = []
    for i, component in enumerate(models):
        if not isinstance(component, dict) or set(component) != {'type', 'pars'}:
            raise ValueError(f'compact_mass_model[{i}] requires exactly type and pars')
        checked.append(_component('compact', component['type'], component['pars'], {}))
    return checked


def _check_point_source(point):
    """Check point-source fields and any partially assembled variability."""
    if not isinstance(point, dict):
        raise ValueError('point_source must be an object')
    if 'light_profile' in point or 'profiles' in point:
        raise ValueError('point_source has no light_profile: use set_source_light_profile for static extended emission, or extrinsic.profiles for microlensing')
    for key in ('x0', 'y0', 'M_tot_unlensed', 'triangle_size'):
        if key in point:
            _finite_light_number('point_source.' + key, point[key])
    if 'triangle_size' in point and point['triangle_size'] <= 0:
        raise ValueError('point_source.triangle_size must be positive')
    if 'variability' in point:
        _check_variability(point['variability'])


def _check_variability(value):
    """Check supplied variability sections without requiring missing branches."""
    # Partial configurations are allowed until validate() checks dependencies.
    if not isinstance(value, dict):
        raise ValueError('point_source.variability must be an object')
    modes = {'intrinsic': ('custom', 'drw'), 'extrinsic': (
        'custom', 'moving_fixed_source', 'moving_fixed_source_custom',
        'moving_variable_source', 'expanding_source')}
    unknown = set(value) - {'intrinsic', 'extrinsic', 'unmicro'}
    if unknown:
        raise ValueError(f'Unknown variability sections: {sorted(unknown)}')
    for branch, allowed in modes.items():
        if branch not in value:
            continue
        settings = value[branch]
        if not isinstance(settings, dict):
            raise ValueError(f'Variability {branch} must be an object')
        if 'type' in settings and settings['type'] not in allowed:
            raise ValueError(f'Unsupported {branch} variability type; choose {allowed}')
        for key in ('pars', 'profiles', 'mean_mag'):
            if key in settings and not isinstance(settings[key], dict):
                raise ValueError(f'{branch}.{key} must be an object')
    for branch in modes:
        if branch in value:
            _strict_variability_branch(branch, value[branch])
    intrinsic_mode = value.get('intrinsic', {}).get('type')
    extrinsic_mode = value.get('extrinsic', {}).get('type')
    if intrinsic_mode == 'drw' and extrinsic_mode == 'moving_variable_source':
        raise ValueError('DRW is incompatible with moving_variable_source in this backend: '
                         'FITS-derived intrinsic curves overwrite the generated DRW curves; '
                         'use custom intrinsic curves or a fixed-profile extrinsic mode')
    if 'unmicro' in value and extrinsic_mode in ('moving_variable_source', 'expanding_source'):
        raise ValueError(f'unmicro is incompatible with {extrinsic_mode}: '
                         'the backend ignores unmicrolensed reverberation in this mode; '
                         'remove unmicro explicitly or select a supported extrinsic mode')
    if 'unmicro' in value:
        if not isinstance(value['unmicro'], dict):
            raise ValueError('unmicro must map instrument names to lag configurations')
        if not value['unmicro']:
            raise ValueError('unmicro must contain band responses; remove the section to disable it')
        for band, settings in value['unmicro'].items():
            if not isinstance(band, str) or not band.strip():
                raise ValueError('unmicro instrument must be a nonempty string')
            if not isinstance(settings, dict) or settings.get('type') not in ('top-hat', 'delta'):
                raise ValueError(f'unmicro.{band} requires a top-hat or delta lag kernel')
            unknown = set(settings) - {'type', 'flux_ratio', 'pars'}
            if unknown:
                raise ValueError(f'unmicro.{band}: unknown parameters {sorted(unknown)}')
            ratio = settings.get('flux_ratio')
            _finite_light_number(f'unmicro.{band}.flux_ratio', ratio)
            if not 0 <= ratio <= 1:
                raise ValueError('unmicro flux_ratio must be between 0 and 1')
            pars = settings.get('pars', {})
            key = 'radius' if settings['type'] == 'top-hat' else 't_peak'
            if not isinstance(pars, dict) or key not in pars:
                raise ValueError(f'unmicro.{band}.pars requires {key}')
            if set(pars) != {key}:
                raise ValueError(f'unmicro.{band}.pars accepts only {key}')
            _finite_light_number(f'unmicro.{band}.{key}', pars[key])
            if pars[key] < 0 or (key == 'radius' and pars[key] == 0):
                raise ValueError(f'unmicro.{band}.{key} is outside its allowed range')


def _validate_config(c):
    """Validate a JSON-compatible configuration without modifying it.

    Raises ValueError for detected missing fields or incompatible inputs.
    Backend asset checks and full physical validation remain outside this helper.
    """
    c, _ = _execution_config(c)
    if 'variability' in c or 'variability' in c.get('source', {}):
        raise ValueError('Variability belongs to point_source, not the root or extended source')
    if 'point_source' in c:
        _check_point_source(c['point_source'])
    errors = []

    def require(mapping, keys, path):
        if not isinstance(mapping, dict):
            errors.append(f'{path}: expected an object')
            return
        for key in keys:
            if key not in mapping or mapping[key] is None:
                errors.append(f'{path}.{key}: required')

    require(c.get('cosmology', {}), ('H0', 'Wm0'), 'cosmology')
    require(c.get('source', {}), ('redshift',), 'source')
    lenses = c.get('lenses', [])
    if not isinstance(lenses, list) or not lenses:
        errors.append('lenses: at least one lens is required')
        lenses = []
    for i, lens in enumerate(lenses):
        require(lens, ('redshift',), f'lenses[{i}]')
        if not isinstance(lens, dict):
            continue
        components = lens.get('mass_model', [])
        if not isinstance(components, list) or not components:
            errors.append(f'lenses[{i}].mass_model: at least one component is required')
            continue
        for j, component in enumerate(components):
            path = f'lenses[{i}].mass_model[{j}]'
            require(component, ('type', 'pars'), path)
            if isinstance(component, dict) and 'type' in component:
                schema = {
                    'sie': ('theta_E', 'q', 'pa', 'x0', 'y0'),
                    'spemd': ('theta_E', 'q', 'pa', 'x0', 'y0', 'gam', 's'),
                    'external_shear': ('g', 'phi', 'x0', 'y0'),
                    'pert': ('filepath', 'Nx', 'Ny', 'xmin', 'xmax', 'ymin', 'ymax'),
                }
                if component['type'] in schema:
                    require(component.get('pars', {}), schema[component['type']], path + '.pars')
    instruments = c.get('instruments', [])
    if not isinstance(instruments, list) or not instruments:
        errors.append('instruments: at least one instrument is required')
        instruments = []
    for i, cam in enumerate(instruments):
        path = f'instruments[{i}]'
        require(cam, ('name', 'ZP', 'field-of-view_xmin', 'field-of-view_xmax',
                      'field-of-view_ymin', 'field-of-view_ymax', 'noise'), path)
        if not isinstance(cam, dict):
            continue
        noise = cam.get('noise', {})
        require(noise, ('type',), path + '.noise')
        if isinstance(noise, dict) and noise.get('type') == 'PoissonNoise':
            require(noise, ('texp', 'Msb'), path + '.noise')
    if 'point_source' in c:
        require(c['point_source'], ('x0', 'y0', 'M_tot_unlensed', 'triangle_size'), 'point_source')
    elif not (isinstance(c.get('source'), dict) and
              isinstance(c['source'].get('light_profile'), dict) and
              any(c['source']['light_profile'].values())):
        errors.append('source: add a light profile or explicitly configure a point source')
    for path, section in [('source', c.get('source', {})),
                          *[(f'lenses[{i}]', lens) for i, lens in enumerate(lenses)]]:
        if not isinstance(section, dict):
            continue
        light = section.get('light_profile', {})
        if not isinstance(light, dict):
            errors.append(f'{path}.light_profile: expected an object')
            continue
        for band, components in light.items():
            if not isinstance(components, list):
                errors.append(f'{path}.light_profile.{band}: expected a list')
                continue
            for j, component in enumerate(components):
                location = f'{path}.light_profile.{band}[{j}]'
                require(component, ('type', 'pars'), location)
                if isinstance(component, dict) and 'type' in component and 'pars' in component:
                    try:
                        _component('light', component['type'], component['pars'], {})
                    except (ValueError, TypeError) as exc:
                        errors.append(f'{location}: {exc}')
    for i, lens in enumerate(lenses):
        if not isinstance(lens, dict):
            continue
        path = f'lenses[{i}]'
        direct = 'compact_mass_model' in lens
        if direct:
            try:
                _checked_compact_models(lens['compact_mass_model'])
            except (ValueError, TypeError) as exc:
                errors.append(f'{path}.compact_mass_model: {exc}')
        bands_with_mass = set()
        light = lens.get('light_profile', {})
        if isinstance(light, dict):
            for band, components in light.items():
                if not isinstance(components, list):
                    continue
                for j, component in enumerate(components):
                    if not isinstance(component, dict) or 'mass-to-light' not in component:
                        continue
                    bands_with_mass.add(band)
                    location = f'{path}.light_profile.{band}[{j}].mass-to-light'
                    mapping = component['mass-to-light']
                    if not isinstance(mapping, dict) or 'upsilon' not in mapping:
                        errors.append(f'{location}: requires an upsilon value')
                        continue
                    if mapping.keys() - {'upsilon', 'upsilon_exp'}:
                        errors.append(f'{location}: unknown parameter names')
                    for key, value in mapping.items():
                        try:
                            _finite_light_number(key, value)
                            if key == 'upsilon' and value < 0:
                                raise ValueError('upsilon must be nonnegative')
                        except ValueError as exc:
                            errors.append(f'{location}: {exc}')
        if len(bands_with_mass) > 1:
            errors.append(f'{path}: mass-to-light must use only ONE instrument band')
        if direct and bands_with_mass:
            errors.append(f'{path}: compact_mass_model and mass-to-light are mutually exclusive')
        if 'point_source' in c and not direct and not bands_with_mass:
            errors.append(f'{path}: point_source requires compact_mass_model or mass-to-light in one band')
    if errors:
        raise ValueError('Incomplete MOLET configuration:\n- ' + '\n- '.join(errors))
    if c['cosmology']['H0'] <= 0:
        raise ValueError('H0 must be positive')
    bands = [i['name'] for i in c['instruments']]
    if len(bands) != len(set(bands)):
        raise ValueError('Duplicate instrument names')
    factor = c.get('output_options', {}).get('super_factor', 10)
    if type(factor) is not int or factor < 1:
        raise ValueError('super_factor must be a positive integer')
    for lens in c['lenses']:
        if not 0 < lens['redshift'] < c['source']['redshift']:
            raise ValueError('Require 0 < lens redshift < source redshift')
    for section in [c['source'], *c['lenses']]:
        unknown = set(section.get('light_profile', {})) - set(bands)
        if unknown:
            raise ValueError(f'Light profiles without an instrument: {unknown}')
    variable = 'variability' in c.get('point_source', {})
    if c.get('output_options', {}).get('output_PS_cutouts') and not variable:
        raise ValueError('output_PS_cutouts requires point-source variability and observation times')
    if variable:
        variability = c['point_source']['variability']
        if not all(variability.get(k, {}).get('type') for k in ('intrinsic', 'extrinsic')):
            raise ValueError('Variability requires both intrinsic and extrinsic sections')
        if 'unmicro' in variability and set(variability['unmicro']) != set(bands):
            raise ValueError('unmicro requires a lag configuration for every instrument and no unknown bands')
        extrinsic = variability['extrinsic']
        if extrinsic['type'].startswith('moving_'):
            required = {'sigma_pec_l', 'sigma_pec_s', 'sigma_disp', 'ra', 'dec'}
            missing = required - extrinsic.get('pars', {}).keys()
            if missing:
                raise ValueError(f'Extrinsic velocity parameters required: {sorted(missing)}')
        if extrinsic['type'] == 'moving_fixed_source':
            profiles = extrinsic.get('profiles', {})
            if not profiles.get('type'):
                raise ValueError('moving_fixed_source requires explicit profiles')
            if profiles['type'] == 'parametric':
                missing = {'r0', 'l0', 'nu', 'shape', 'incl', 'orient'} - profiles.keys()
                if missing:
                    raise ValueError(f'Extrinsic profile parameters required: {sorted(missing)}')
        intrinsic = variability['intrinsic']
        if intrinsic['type'] == 'drw':
            if any(k not in intrinsic for k in ('N_in', 'absolute_i_mag', 'mean_mag')):
                raise ValueError('DRW requires N_in, absolute_i_mag and mean_mag')
            if intrinsic['N_in'] < 1 or not set(bands) <= set(intrinsic['mean_mag']):
                raise ValueError('DRW requires N_in >= 1 and mean_mag for every band')
    for cam in c['instruments']:
        for axis in ('x', 'y'):
            if cam[f'field-of-view_{axis}min'] >= cam[f'field-of-view_{axis}max']:
                raise ValueError('Field-of-view minima must be smaller than maxima')
        if variable and not cam.get('time'):
            raise ValueError('Variability requires instrument time arrays')
        times = cam.get('time', [])
        if not isinstance(times, list):
            raise ValueError('Observation times must be an array')
        for time in times:
            _finite_light_number('Observation time', time)
        if times != sorted(times):
            raise ValueError('Observation times must be nondecreasing')
        if cam.get('noise', {}).get('type') == 'PoissonNoise' and cam['noise']['texp'] <= 0:
            raise ValueError('PoissonNoise texp must be positive')

    _check_variability_bands(c)



def _mass_component(model, pars, parameters):
    """Validate and copy the explicit parameters of one macro mass component."""
    required = {
        'sie': {'theta_E', 'q', 'pa', 'x0', 'y0'},
        'spemd': {'theta_E', 'q', 'pa', 'x0', 'y0', 'gam', 's'},
        'external_shear': {'g', 'phi', 'x0', 'y0'},
        'pert': {'filepath', 'Nx', 'Ny', 'xmin', 'xmax', 'ymin', 'ymax', 'scale_factor'},
    }
    if model not in required:
        raise ValueError(f'Unknown mass model {model!r}; choose {sorted(required)}')
    if pars is not None and not isinstance(pars, dict):
        raise TypeError('pars must be a dictionary or None')
    supplied = deepcopy(dict(pars or {}, **parameters))
    missing = required[model] - supplied.keys()
    unknown = supplied.keys() - required[model]
    if missing or unknown:
        raise ValueError(f'{model}: missing parameters {sorted(missing)}; '
                         f'unknown parameters {sorted(unknown)}. '
                         'Supply every model parameter explicitly on each call.')
    for key, value in supplied.items():
        if key == 'filepath':
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError('filepath must be a nonempty path')
            supplied[key] = str(value)
        elif isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{key} must be a finite real number')
    if model == 'pert':
        for key in ('Nx', 'Ny'):
            if supplied[key] < 1 or int(supplied[key]) != supplied[key]:
                raise ValueError(f'{key} must be a positive integer')
            supplied[key] = int(supplied[key])
        for axis in ('x', 'y'):
            if supplied[axis + 'min'] >= supplied[axis + 'max']:
                raise ValueError('Grid minima must be smaller than maxima')
    return {'type': model, 'pars': supplied}



def _check_compact_prescription(lens, prescription):
    """Reject an incompatible existing prescription before a setter mutates a lens."""
    if prescription == 'mass-to-light':
        conflict = 'compact_mass_model' in lens
    else:
        conflict = any('mass-to-light' in profile
                       for profiles in lens.get('light_profile', {}).values()
                       for profile in profiles)
    if conflict:
        raise ValueError('compact_mass_model and mass-to-light are mutually exclusive '
                         'for the same lens; explicitly remove the existing prescription first')


def _required_fields(value, keys, path):
    """Require an object with explicit, non-null fields."""
    if not isinstance(value, dict):
        raise ValueError(f'{path} must be an object')
    missing = [k for k in keys if k not in value or value[k] is None]
    if missing:
        raise ValueError(f'{path} requires: {", ".join(sorted(missing))}')


def _number(value, path, minimum=None, strict=False, maximum=None, integer=False):
    """Check a finite scalar and optional range/integer constraints."""
    _finite_light_number(path, value)
    if integer and not isinstance(value, Integral):
        raise ValueError(f'{path} must be an integer')
    if minimum is not None and (value < minimum or (strict and value == minimum)):
        raise ValueError(f'{path} must be {">" if strict else ">="} {minimum}')
    if maximum is not None and value > maximum:
        raise ValueError(f'{path} must be <= {maximum}')


def _time_array(value, path):
    """Require at least two strictly increasing finite epochs."""
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError(f'{path} requires at least two time samples')
    for item in value:
        _number(item, path)
    if any(b <= a for a, b in zip(value, value[1:])):
        raise ValueError(f'{path} must be strictly increasing')


def _strict_variability_branch(branch, value):
    """Validate complete model parameters; cross-section dependencies are deferred."""
    path = 'variability.' + branch
    _required_fields(value, ['type'], path)
    mode = value['type']
    if branch == 'intrinsic':
        allowed = {'type', 'scale_factor'}
        if mode == 'drw':
            allowed |= {'N_in', 'absolute_i_mag', 'mean_mag'}
            _required_fields(value, ['N_in', 'absolute_i_mag', 'mean_mag'], 'DRW')
            _number(value['N_in'], 'DRW.N_in', minimum=1, integer=True)
            _number(value['absolute_i_mag'], 'DRW.absolute_i_mag')
            if not isinstance(value['mean_mag'], dict) or not value['mean_mag']:
                raise ValueError('DRW.mean_mag requires a nonempty band-to-magnitude mapping')
            for band, mag in value['mean_mag'].items():
                _number(mag, f'DRW.mean_mag.{band}')
        if set(value) - allowed:
            raise ValueError(f'{path}: unsupported or unused fields {sorted(set(value)-allowed)}')
        _number(value.get('scale_factor', 1), path+'.scale_factor', minimum=0, strict=True)
        return
    if mode == 'custom':
        if set(value) != {'type'}:
            raise ValueError('custom extrinsic accepts only type; other fields would be ignored')
        return
    _required_fields(value, ['microlens_mass', 'Nex'], path)
    _number(value['microlens_mass'], path+'.microlens_mass', minimum=0, strict=True)
    _number(value['Nex'], path+'.Nex', minimum=1, integer=True)
    common = {'type', 'microlens_mass', 'Nex'}
    if mode.startswith('moving_'):
        _required_fields(value, ['pars'], path)
        pars = value['pars']
        velocities = {'sigma_pec_l', 'sigma_pec_s', 'sigma_disp', 'ra', 'dec'}
        _required_fields(pars, velocities, path+'.pars')
        if set(pars) - velocities:
            raise ValueError(f'{path}.pars: unknown velocity parameters')
        for k in velocities:
            _number(pars[k], path+'.pars.'+k)
        for k in velocities - {'ra', 'dec'}:
            _number(pars[k], path+'.pars.'+k, minimum=0)
        _number(pars['ra'], path+'.pars.ra', minimum=0, maximum=360)
        if pars['ra'] == 360:
            raise ValueError('ra must satisfy 0 <= ra < 360 degrees')
        _number(pars['dec'], path+'.pars.dec', minimum=-90, maximum=90)
        common.add('pars')
    if mode == 'moving_fixed_source':
        _required_fields(value, ['profiles'], path)
        p = value['profiles']
        _required_fields(p, ['type', 'shape', 'incl', 'orient'], path+'.profiles')
        kinds = {'parametric': {'r0', 'l0', 'nu'}, 'ss_disc': {'mbh', 'fedd', 'eta'},
                 'vector': {'rhalf'}, 'custom': set()}
        if not isinstance(p['type'], str) or p['type'] not in kinds:
            raise ValueError('Unknown microlensing profile size type')
        # The native implementation calls JsonCpp asString on the rhalf array.
        if p['type'] == 'vector':
            raise ValueError('profiles.type=vector is unsafe in the installed backend (rhalf array converted with asString); use parametric or fixed FITS profiles')
        shapes = {'uniform': set(), 'gaussian': set(), 'exponential': set(),
                  'gaussian_hole': {'Rin'}, 'thermal_hole': set(), 'wavy': {'a'},
                  'custom': {'filename', 'profPixSizePhys'}}
        if not isinstance(p['shape'], str) or p['shape'] not in shapes:
            raise ValueError('Unknown microlensing profile shape')
        if (p['type'] == 'custom') != (p['shape'] == 'custom'):
            raise ValueError('custom profile size type and shape must be used together')
        fields = {'type', 'shape', 'incl', 'orient'} | kinds[p['type']] | shapes[p['shape']]
        _required_fields(p, fields, path+'.profiles')
        if set(p) - fields:
            raise ValueError(f'Unknown or unused profile parameters: {sorted(set(p)-fields)}')
        for k in fields - {'type', 'shape', 'filename'}:
            _number(p[k], path+'.profiles.'+k)
        for k in fields & {'r0', 'l0', 'mbh', 'fedd', 'eta', 'profPixSizePhys'}:
            _number(p[k], path+'.profiles.'+k, minimum=0, strict=True)
        _number(p['incl'], 'profiles.incl', minimum=0, maximum=90)
        if p['incl'] == 90:
            raise ValueError('profiles.incl must be < 90 degrees')
        if 'Rin' in p:
            _number(p['Rin'], 'profiles.Rin', minimum=0)
        if 'filename' in p and (not isinstance(p['filename'], (str, Path)) or not str(p['filename']).strip()):
            raise ValueError('profiles.filename must be a nonempty path')
        common.add('profiles')
    elif mode in ('moving_fixed_source_custom', 'moving_variable_source'):
        metadata = set(value) - common
        if not metadata:
            raise ValueError(f'{mode} requires per-instrument FITS metadata')
        for band in metadata:
            entry = value[band]
            fields = {'pixSize'} if mode == 'moving_fixed_source_custom' else {'pixSize', 'Nx', 'Ny', 'time'}
            _required_fields(entry, fields, f'{path}.{band}')
            # Nx/Ny are accepted for fixed profiles as explicit file metadata.
            if set(entry) - (fields | {'Nx', 'Ny'}):
                raise ValueError(f'{path}.{band}: unknown FITS metadata')
            _number(entry['pixSize'], f'{band}.pixSize', minimum=0, strict=True)
            for k in ('Nx', 'Ny'):
                if k in entry:
                    _number(entry[k], f'{band}.{k}', minimum=1, integer=True)
            if mode == 'moving_variable_source':
                _time_array(entry['time'], f'{band}.time')
        common |= metadata
    elif mode == 'expanding_source':
        fields = {'incl', 'orient', 'v_expand', 'fractional_increase', 'size_cutoff'}
        _required_fields(value, fields, path)
        for k in fields - {'v_expand'}:
            _number(value[k], path+'.'+k)
        _number(value['incl'], path+'.incl', minimum=0, maximum=90)
        if value['incl'] == 90:
            raise ValueError('incl must be < 90 degrees')
        for k in ('fractional_increase', 'size_cutoff'):
            _number(value[k], path+'.'+k, minimum=0, strict=True)
        if not isinstance(value['v_expand'], dict) or not value['v_expand']:
            raise ValueError('v_expand requires a nonempty band-to-speed mapping')
        for band, speed in value['v_expand'].items():
            _number(speed, f'v_expand.{band}', minimum=0, strict=True)
        _number(value['size_cutoff'], path+'.size_cutoff', maximum=7)
        common |= fields
    if set(value) - common:
        raise ValueError(f'{path}: unsupported or unused fields {sorted(set(value)-common)}')


def _check_variability_bands(config):
    """Check band-dependent variability fields once instruments are configured."""
    v = config.get('point_source', {}).get('variability')
    if not v:
        return
    bands = {cam['name'] for cam in config['instruments']}
    ex = v['extrinsic']
    if v['intrinsic']['type'] == 'drw' and set(v['intrinsic']['mean_mag']) != bands:
        raise ValueError('DRW.mean_mag must contain exactly the configured instrument bands')
    if ex['type'] in ('moving_fixed_source_custom', 'moving_variable_source'):
        supplied = set(ex) - {'type', 'microlens_mass', 'Nex', 'pars'}
        if supplied != bands:
            raise ValueError('FITS metadata must cover exactly the configured instrument bands')
    if ex['type'] == 'expanding_source' and set(ex['v_expand']) != bands:
        raise ValueError('v_expand must cover exactly the configured instrument bands')
    for cam in config['instruments']:
        times = cam['time']
        if max(times) <= min(times):
            raise ValueError('Variability requires a positive observation time span in each band')
        if ex['type'] == 'moving_variable_source':
            snapshots = ex[cam['name']]['time']
            if snapshots[0] > times[0] or snapshots[-1] < times[-1]:
                raise ValueError('Snapshot times must cover the observing interval; backend checks additional delay padding')


def _check_variability_assets(config, asset_dir, automatic_unity=False):
    """Check required files and custom curve structure before creating a run directory.

    FITS existence is checked, not FITS pixels/headers. Macro-image ordering,
    map availability and extra time-delay coverage require backend results.
    """
    v = config.get('point_source', {}).get('variability')
    if not v:
        return
    base = Path(asset_dir) if asset_dir is not None else None
    counts = {'intrinsic': set(), 'extrinsic': set()}
    image_counts = set()
    def file(name):
        target = base / name if base is not None else None
        if target is None or not target.is_file():
            raise FileNotFoundError(f'Required variability input file: {name}; supply process(input_files=...)')
        return target
    def curve(lc, label, times, extrinsic):
        _required_fields(lc, ['time', 'signal'], label)
        _time_array(lc['time'], label+'.time')
        for key in ('signal', 'dsignal'):
            if key not in lc:
                if extrinsic:
                    raise ValueError(f'{label} requires dsignal (use zeros if appropriate)')
                continue
            if not isinstance(lc[key], list) or len(lc[key]) != len(lc['time']):
                raise ValueError(f'{label}.{key} must match time length')
            for val in lc[key]:
                _number(val, label+'.'+key, minimum=0 if extrinsic or key=='dsignal' else None)
        if lc['time'][0] > times[0] or lc['time'][-1] < times[-1]:
            raise ValueError(f'{label}: light curve must cover observation times; backend checks lens-delay padding')
    for cam in config['instruments']:
        band, times = cam['name'], cam['time']
        for branch in ('intrinsic', 'extrinsic'):
            if branch == 'extrinsic' and automatic_unity:
                continue
            if v[branch]['type'] != 'custom':
                continue
            name = f'{band}_LC_{branch}.json'
            data = _load(file(name))
            if not isinstance(data, list) or not data:
                raise ValueError(f'{name} must be a nonempty array')
            if branch == 'extrinsic':
                image_counts.add(len(data))
            groups = [data] if branch == 'intrinsic' else data
            nonempty = False
            for group in groups:
                if not isinstance(group, list):
                    raise ValueError(f'{name}: expected an array of realizations per image')
                if group:
                    nonempty = True
                    counts[branch].add(len(group))
                for j, lc in enumerate(group):
                    curve(lc, f'{name}[{j}]', times, branch == 'extrinsic')
                    if branch == 'intrinsic' and 'unmicro' in v:
                        _check_unmicro_sampling(lc['time'], v['unmicro'][band], name)
            if not nonempty:
                raise ValueError(f'{name}: all macro-image curve arrays are empty; use unity curves')
        ex = v['extrinsic']
        if ex['type'] == 'moving_fixed_source_custom':
            file(f'cs_{band}.fits')
        elif ex['type'] == 'moving_variable_source':
            for i in range(len(ex[band]['time'])):
                file(f'vs_{band}/{i:04d}.fits')
    if len(image_counts) > 1:
        raise ValueError('Custom extrinsic macro-image counts must agree across bands')
    for branch, values in counts.items():
        if len(values) > 1:
            raise ValueError(f'Custom {branch} realization counts must agree across images/bands')
    ex = v['extrinsic']
    if ex['type'] == 'moving_fixed_source' and ex['profiles']['shape'] == 'custom':
        filename = Path(ex['profiles']['filename']).expanduser()
        # Native profile filename is resolved in the run working directory.
        if filename.is_absolute():
            if not filename.is_file():
                raise FileNotFoundError(filename)
        elif filename.parts and filename.parts[0] == 'input_files':
            file(Path(*filename.parts[1:]))
        else:
            raise ValueError('Custom profiles.filename must be absolute or start with input_files/')


def _updated_variability(current, branch, value):
    """Build and check a branch replacement without mutating existing input."""
    candidate = deepcopy(current)
    if not isinstance(candidate, dict):
        raise ValueError('point_source.variability must be an object')
    if branch == 'unmicro':
        if 'unmicro' in candidate and not isinstance(candidate['unmicro'], dict):
            raise ValueError('unmicro must map instrument names to lag configurations')
        candidate.setdefault('unmicro', {}).update(deepcopy(value))
    else:
        candidate[branch] = deepcopy(value)
    _check_variability(candidate)
    return candidate


def _check_unmicro_sampling(times, settings, label):
    """Check assumptions of the native index-based lag convolution on custom input."""
    step = times[1] - times[0]
    if any(not math.isclose(b-a, step, rel_tol=1e-7, abs_tol=1e-10)
           for a, b in zip(times, times[1:])):
        raise ValueError(f'{label}: unmicro requires uniformly sampled intrinsic curves')
    support = (settings['pars']['radius']/25.9 if settings['type'] == 'top-hat'
               else settings['pars']['t_peak'])
    if support > times[-1] - times[0]:
        raise ValueError(f'{label}: unmicro kernel support exceeds the intrinsic curve duration')


def _create_run_directory(run_dir, protected=()):
    """Create or empty the requested run directory without following child links."""
    import tempfile
    import shutil
    if run_dir is None:
        return Path(tempfile.mkdtemp(prefix='molet_')).resolve()
    requested = Path(run_dir).expanduser()
    if requested.is_symlink():
        raise ValueError('run_dir must not be a symbolic link')
    run = requested.resolve()
    if any(ch.isspace() for ch in str(run)):
        raise ValueError('MOLET driver does not support whitespace in run paths')
    for location in [Path.home(), Path.cwd(), *protected]:
        if location is not None:
            location = Path(location).resolve()
            if run == location or run in location.parents:
                raise ValueError(f'run_dir would overwrite a protected source or working directory: {location}')
    if run.exists() and not run.is_dir():
        raise NotADirectoryError(f'run_dir is not a directory: {run}')
    run.mkdir(parents=True, exist_ok=True)
    for child in run.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    return run


def _execution_config(config):
    """Resolve only an omitted extrinsic branch; never repair explicit invalid input."""
    resolved = deepcopy(config)
    point = resolved.get('point_source', {})
    variability = point.get('variability') if isinstance(point, dict) else None
    automatic = (isinstance(variability, dict) and 'intrinsic' in variability
                 and 'extrinsic' not in variability)
    if automatic:
        variability['extrinsic'] = {'type': 'custom'}
    return resolved, automatic


def _unity_driver_text(driver):
    """Verify the installed driver has the expected pre-combination hook."""
    marker = '    # Perform check of light curve lengths:'
    text = Path(driver).read_text()
    if text.count(marker) != 1 or 'bin/vkl_point_source ' not in text or 'bin/var_drw ' not in text:
        raise ValueError('Automatic unity curves require the supported MOLET driver layout; '
                         'provide explicit custom extrinsic curves for this driver')
    return text, marker


def _prepare_unity_driver(driver_text, run, config):
    """Write a run-local driver with a unity-curve hook; leave MOLET untouched."""
    import sys
    import shlex
    text, marker = driver_text
    helper = run / 'input_files' / '_generate_unity.py'
    helper.write_text('import sys\n' + f'sys.path.insert(0, {str(Path(__file__).parent)!r})\n'
                      'from molet_auxiliary import _generate_unity_curves\n'
                      + f'_generate_unity_curves({str(run)!r})\n')
    for cam in config['instruments']:
        # Native initialization checks existence before multiple_images is computed.
        (run/'input_files'/f"{cam['name']}_LC_extrinsic.json").write_text('[]\n')
    invocation = f'    {shlex.quote(sys.executable)} {shlex.quote(str(helper))} || exit 1\n'
    adapted = run/'input_files'/'_molet_driver_unity'
    adapted.write_text(text.replace(marker, invocation+marker, 1))
    adapted.chmod(0o700)
    return adapted


def _generate_unity_curves(run):
    """Generate one unity realization per actual macro image, with safe coverage."""
    run = Path(run)
    config = _load(run/'molet_input.json')
    images = _load(run/'output'/'multiple_images.json')
    if not isinstance(images, list) or not images:
        raise ValueError('Cannot generate unity curves: MOLET found no macro images')
    delays = []
    for entry in images:
        _required_fields(entry, ['dt'], 'macro image')
        _number(entry['dt'], 'macro image dt')
        delays.append(entry['dt'])
    lo = min(cam['time'][0] for cam in config['instruments'])
    hi = max(cam['time'][-1] for cam in config['instruments'])
    max_shift = max(delays) - min(delays)
    intrinsic = config['point_source']['variability']['intrinsic']
    for cam in config['instruments']:
        band = cam['name']
        location = run/('input_files' if intrinsic['type']=='custom' else 'output')
        curves = _load(location/f'{band}_LC_intrinsic.json')
        if not isinstance(curves, list) or not curves:
            raise ValueError(f'{band}: no intrinsic realizations')
        for lc in curves:
            _time_array(lc['time'], f'{band} intrinsic times')
            # C++ interpolation uses t+delay and a strict upper endpoint.
            if lc['time'][0] > lo or lc['time'][-1] <= hi+max_shift:
                raise ValueError(f'{band}: intrinsic times must cover [{lo}, {hi+max_shift}] '
                                 'with a strictly later final sample for lens-delay interpolation')
            unmicro = config['point_source']['variability'].get('unmicro')
            if unmicro:
                _check_unmicro_sampling(lc['time'], unmicro[band], band)
        curve = {'time': [lo-1, hi+max_shift+1], 'signal': [1.0,1.0], 'dsignal':[0.0,0.0]}
        (run/'input_files'/f'{band}_LC_extrinsic.json').write_text(
            json.dumps([[curve] for _ in images], indent=2)+'\n')
    print(f'Automatic no-microlensing mode: unity curves for {len(images)} macro images.')
