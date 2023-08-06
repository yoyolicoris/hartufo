import csv
from pathlib import Path
import warnings
import random
from abc import abstractmethod
from numbers import Number
from xml.etree.ElementTree import parse
import numpy as np
from openpyxl import load_workbook
from pymatreader import read_mat
from scipy import io


_CIPIC_ANTHROPOMETRY_NAMES = {
    'weight': ('weight',),
    'age': ('age',),
    'sex': ('sex',), 
    'head-torso': (
        'head width',
        'head height',
        'head depth',
        'pinna offset down',
        'pinna offset back',
        'neck width',
        'neck height',
        'neck depth',
        'torso top width',
        'torso top height',
        'torso top depth',
        'shoulder width',
        'head offset forward',
        'height',
        'seated height',
        'head circumference',
        'shoulder circumference',
    ),
    'pinna-size': (
        'cavum concha height',
        'cymba concha height',
        'cavum concha width',
        'fossa height',
        'pinna height',
        'pinna width',
        'intertragal incisure width',
        'cavum concha depth',
    ),
    'pinna-angle': (
        'pinna rotation angle',
        'pinna flare angle',
    ),
}


def str2float(value):
    try:
        return float(value)
    except ValueError:
        return np.nan
    

class DataQuery:

    def __init__(
        self,
        collection_id: str,
    ):
        self.collection_id = collection_id
        self.allowed_keys = ['subject', 'side', 'collection']


    def validate_specification(self, spec):
        def validate_dict(given_dict, allowed_keys, key=''):
            if key:
                try:
                    given_dict = given_dict[key]
                except KeyError:
                    return
            unknown_keys = sorted([x for x in set(given_dict.keys()).difference(allowed_keys) if not isinstance(x, Number)])
            if unknown_keys:
                raise ValueError(f'Unknown specifier{"s" if len(unknown_keys) > 1 else ""} "{", ".join(unknown_keys)}" in {key if key else "specification"}')
        validate_dict(spec, self.allowed_keys)
        validate_dict(spec, ('side', 'domain', 'fundamental_angles', 'orthogonal_angles', 'scale_factor', 'samplerate', 'length', 'min_phase', 'exclude', 'preprocess', 'transform'), 'hrirs')
        validate_dict(spec, ('preprocess', 'transform'), 'subject')
        validate_dict(spec, ('preprocess', 'transform'), 'side')
        validate_dict(spec, ('preprocess', 'transform'), 'collection')
        validate_dict(spec, ('side', 'rear', 'preprocess', 'transform'), 'image')
        validate_dict(spec, ('side', 'select', 'partial', 'preprocess', 'transform'), 'anthropometry')


    def specification_based_ids(self, specification, include_subjects=None, exclude_subjects=None):
        self.validate_specification(specification)
        all_sides = {}
        for key in ('hrirs', 'image', 'anthropometry'):
            side = specification.get(key, {}).get('side')
            if side is not None:
                all_sides[key] = side
        if len(set(all_sides.values())) > 1:
            warn_strings = [f'{k} ("{v}")' for k, v in all_sides.items()]
            warnings.warn(f"Different sides requested for {', '.join(warn_strings[:-1])} and {warn_strings[-1]}")
        default_side = list(all_sides.values())[0] if len(all_sides) > 0 else 'any'

        if include_subjects is not None and len(include_subjects) == 0:
            return []

        separate_ids = []
        if 'image' in specification.keys():
            side = specification['image'].get('side', default_side)
            rear = specification['image'].get('rear')
            exclude = specification['image'].get('exclude', exclude_subjects)
            separate_ids.append(set(self.image_ids(side, rear, exclude)))
        if 'anthropometry' in specification.keys():
            side = specification['anthropometry'].get('side', default_side)
            exclude = specification['anthropometry'].get('exclude', exclude_subjects)
            optional_kwargs = {k: v for k, v in specification['anthropometry'].items() if k in ('select', 'partial')}
            separate_ids.append(set(self.anthropometry_ids(side, **optional_kwargs, exclude=exclude)))
        if 'hrirs' in specification.keys():
            side = specification['hrirs'].get('side', default_side)
            exclude = specification['hrirs'].get('exclude', exclude_subjects)
            separate_ids.append(set(self.hrir_ids(side, exclude)))

        ids = sorted(set.intersection(*separate_ids))
        if include_subjects is None:
            return ids
        if len(ids) > 0 and include_subjects == 'first':
            return [ids[0]]
        if len(ids) > 0 and include_subjects == 'last':
            return [ids[-1]]
        if len(ids) > 0 and include_subjects == 'random':
            return [random.choice(ids)]
        return [(i, s) for i, s in ids if i in include_subjects]


    @staticmethod
    def _id_helper(side, id_fn, exclude, default_exclude):
        if side.startswith('both') or side.startswith('any'):
            left_ids = id_fn('left')
            right_ids = id_fn('right')
            if side.endswith('left'):
                sides = ('left', 'mirrored-right')
            elif side.endswith('right'):
                sides = ('mirrored-left', 'right')
            else:
                sides = ('left', 'right')
            if side.startswith('both'):
                both_ids = sorted(set(left_ids).intersection(right_ids))
                ids = [(i, s) for i in both_ids for s in sides]
            else:
                ids = sorted([(i, sides[0]) for i in left_ids] + [(i, sides[1]) for i in right_ids])
        elif side in ['left', 'right']:
            ids = [(i, side) for i in id_fn(side)]
        else:
            raise ValueError(f'Unknown side "{side}"')
        if exclude is None:
            exclude = default_exclude
        return [(i, s) for i, s in ids if i not in exclude]


class HrirDataQuery(DataQuery):

    _default_hrirs_exclude = ()


    def __init__(self,
        sofa_directory_path: str = '',
        variant_key: str = '',
        **kwargs,
    ):
        super().__init__(**kwargs)
        if sofa_directory_path:
            self.sofa_directory_path = Path(sofa_directory_path)
            self.allowed_keys += ['hrirs']
            self._variant_key = variant_key


    def hrir_ids(self, side, exclude=None):
        return self._id_helper(side, self._all_hrir_ids, exclude, self._default_hrirs_exclude)


    @abstractmethod
    def _all_hrir_ids(self, side):
        pass


class AnthropometryDataQuery(DataQuery):

    _default_anthropometry_exclude = ()

    def __init__(self,
        anthropometry_path: str = '',
        **kwargs,
    ):
        super().__init__(**kwargs)
        if anthropometry_path:
            self.anthropometry_path = Path(anthropometry_path)
            self._load_anthropometry(self.anthropometry_path)
            self.allowed_keys += ['anthropometry']
        else:
            self._anthropometric_ids = np.array([], dtype=int)
            self._anthropometry = {}


    def anthropometry_ids(self, side, select=None, partial=False, exclude=None):
        return self._id_helper(side, lambda s: self._all_anthropometry_ids(s, select, partial), exclude, self._default_anthropometry_exclude)


    def _all_anthropometry_ids(self, side, select, partial):
        selected_anthropometry = self._anthropometry_values(side, select)
        if partial:
            allowed_id_mask = (~np.isnan(selected_anthropometry)).any(axis=1)
        else:
            allowed_id_mask = (~np.isnan(selected_anthropometry)).all(axis=1)
        return self._anthropometric_ids[allowed_id_mask]


    @property
    def allowed_anthropometry_selection(self):
        return tuple(self._anthropometry.keys())


    @abstractmethod
    def _load_anthropometry(self, anthropometry_path):
        pass


    def _selection_validator(self, select):
        if select is None:
            select = self.allowed_anthropometry_selection
        elif isinstance(select, str):
            select = (select,)
        unknown_select = sorted(set(select) - set(self.allowed_anthropometry_selection))
        if unknown_select:
            raise ValueError(f'Unknown selection "{unknown_select}". Choose one or more from "{self.allowed_anthropometry_selection}"')
        # if 'pinna-size' not in select and 'pinna-angle' not in select:
        #     if side is not 'any':
        #         print(f'Side "{side}" is irrelevant for this anthropometry selection "{", ".join(select)}"')
        return select


    @property
    @abstractmethod
    def _anthropometry_names(self):
        pass


    def anthropometry_names(self, select=None):
        select = self._selection_validator(select)
        return [n for s in select for n in self._anthropometry_names[s]]


    def _anthropometry_values(self, side, select=None):
        select = self._selection_validator(select)
        if side not in ['left', 'right', 'mirrored-left', 'mirrored-right']:
            raise ValueError(f'Unknown side selector "{side}"')
        real_side = side.split('mirrored-')[-1]
        return np.column_stack([self._anthropometry[s][real_side] if s.startswith('pinna') else self._anthropometry[s] for s in select])


class ImageDataQuery(DataQuery):

    _default_images_exclude = ()


    def __init__(self,
        image_directory_path: str = '',
        **kwargs,
    ):
        super().__init__(**kwargs)
        if image_directory_path:
            self.image_directory_path = Path(image_directory_path)
            self.allowed_keys += ['image']


    def image_ids(self, side, rear=False, exclude=None):
        return self._id_helper(side, lambda s: self._all_image_ids(s, rear), exclude, self._default_images_exclude)


    @abstractmethod
    def _all_image_ids(self, side, rear):
        pass


class MeshDataQuery(DataQuery):

    _default_mesh_exclude = ()


    def __init__(self,
        mesh_directory_path: str = '',
        **kwargs,
    ):
        super().__init__(**kwargs)
        if mesh_directory_path:
            self.mesh_directory_path = Path(mesh_directory_path)
            self.allowed_keys += ['3d-model']


    def mesh_ids(self, side, exclude=None):
        return self._id_helper(side, lambda s: self._all_mesh_ids(s), exclude, self._default_mesh_exclude)


    @abstractmethod
    def _all_mesh_ids(self):
        pass


class CipicDataQuery(HrirDataQuery, AnthropometryDataQuery, ImageDataQuery):

    def __init__(self, sofa_directory_path=None, image_directory_path=None, anthropometry_matfile_path=None):
        super().__init__(collection_id='cipic', sofa_directory_path=sofa_directory_path, image_directory_path=image_directory_path, anthropometry_path=anthropometry_matfile_path)
        self._default_hrirs_exclude = (21, 165) # KEMAR dummy
        self._default_images_exclude = (21,) # KEMAR dummy
        self._default_anthropometry_exclude = (21, 165) # KEMAR dummy


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[1]) for x in self.sofa_directory_path.glob('subject_*.sofa')])
    

    def _all_image_ids(self, side, rear):
        all_globs = (self.image_directory_path.rglob('*'+suffix) for suffix in self._image_suffix(side, rear))
        return sorted(set([int(x.stem.split('_')[0]) for glob in all_globs for x in glob]))


    @staticmethod
    def _image_suffix(side, rear):
        if rear:
            return ('_{}_rear.jpg'.format(side.split('-')[-1]), '_back.jpg')
        else:
            return ('_{}_side.jpg'.format(side.split('-')[-1]),)


    def _load_anthropometry(self, anthropometry_path):
        # cm & rad
        mat_anth = io.loadmat(anthropometry_path, squeeze_me=True)
        self._anthropometric_ids = mat_anth['id'].astype(int)
        self._anthropometry = {
            'weight': mat_anth['WeightKilograms'].reshape(-1, 1),
            'age': mat_anth['age'].reshape(-1, 1),
            'sex': np.select([mat_anth['sex'] == 'M', mat_anth['sex'] == 'F'], [0, 1], default=np.nan).reshape(-1, 1),
            'head-torso': 10*mat_anth['X'],
            'pinna-size': {'left': 10*mat_anth['D'][:, :8], 'right': 10*mat_anth['D'][:, 8:]},
            'pinna-angle': {'left': np.rad2deg(mat_anth['theta'][:, :2]), 'right': np.rad2deg(mat_anth['theta'][:, 2:])},
        }


    @property
    def _anthropometry_names(self):
        return _CIPIC_ANTHROPOMETRY_NAMES


class AriDataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path=None, anthropometry_matfile_path=None):
        super().__init__(collection_id='ari', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_matfile_path)
        self._default_hrirs_exclude = (10, 22, 826) # missing 1, 2, and 2 measurement positions


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_nh')[1]) for x in self.sofa_directory_path.glob('hrtf [bc]_nh*.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # cm & rad
        mat_anth = io.loadmat(anthropometry_path, squeeze_me=True)
        self._anthropometric_ids = mat_anth['id'].astype(int)
        self._anthropometry = {
            'weight': mat_anth['WeightKilograms'].reshape(-1, 1),
            'age': mat_anth['age'].reshape(-1, 1),
            'sex': np.select([mat_anth['sex'] == 'M', mat_anth['sex'] == 'F'], [0, 1], default=np.nan).reshape(-1, 1),
            'head-torso': 10*mat_anth['X'],
            'pinna-size': {'left': 10*np.column_stack((mat_anth['D'][:, :8], mat_anth['A'][:, :9])), 'right': 10*np.column_stack((mat_anth['D'][:, 8:16], mat_anth['A'][:, 9:]))},
            'pinna-angle': {'left': np.rad2deg(mat_anth['theta'][:, :2]), 'right': np.rad2deg(mat_anth['theta'][:, 2:])},
        }


    @property
    def _anthropometry_names(self):
        anthropometry_names = _CIPIC_ANTHROPOMETRY_NAMES
        anthropometry_names['pinna-size'] += ('a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8', 'a9')
        return anthropometry_names


class ListenDataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path=None, anthropometry_directory_path=None, hrtf_type='compensated'):
        if hrtf_type == 'raw':
            self._hrtf_type_char = 'R'
        elif hrtf_type == 'compensated':
            self._hrtf_type_char = 'C'
        else:
            raise ValueError(f'Unknown HRTF type "{hrtf_type}"')
        super().__init__(collection_id='listen', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_directory_path, variant_key=f'{hrtf_type}/44100')


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[1]) for x in (self.sofa_directory_path / self._variant_key).glob(f'IRC_????_{self._hrtf_type_char}_44100.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # mm & deg
        anthropometry_ids = []
        self._anthropometry = {'sex': [], 'head-torso': [], 'pinna-size': {'left': [], 'right': []}, 'pinna-angle': {'left': [], 'right': []}}
        for xml_path in sorted(anthropometry_path.glob('*.xml')):
            root = parse(xml_path).getroot()
            anthropometry_ids.append(1000+int(root.find('./Subject/ID').text.strip().strip('IRC')))
            sex = root.find('./Subject/Sex').text.strip()
            self._anthropometry['sex'].append(0 if sex == 'Male' else 1 if sex == 'Female' else np.nan)
            head_torso = [str2float(el.text.strip()) for el in root.find('.//Head_and_Torso')]
            self._anthropometry['head-torso'].append(head_torso)
            pinna_size = {'left': [], 'right': []}
            pinna_angle = {'left': [], 'right': []}
            for el in root.find('.//Pinna'):
                if el.tag == 'Side':
                    side = el.text.strip().lower()
                elif el.tag[-3:-1] == '_t':
                    pinna_angle[side].append(str2float(el.text.strip()))
                else:
                    pinna_size[side].append(str2float(el.text.strip()))
            for side in ('left', 'right'):
                self._anthropometry['pinna-size'][side].append(pinna_size[side])
                self._anthropometry['pinna-angle'][side].append(pinna_angle[side])
        self._anthropometric_ids = np.array(anthropometry_ids)


    @property
    def _anthropometry_names(self):
        return {
            'sex': _CIPIC_ANTHROPOMETRY_NAMES['sex'],
            'head-torso': _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][:12] + _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][15:],
            'pinna-size': _CIPIC_ANTHROPOMETRY_NAMES['pinna-size'],
            'pinna-angle': _CIPIC_ANTHROPOMETRY_NAMES['pinna-angle'],
        }


class BiLiDataQuery(HrirDataQuery):

    def __init__(self, sofa_directory_path, samplerate=96000, hrtf_type='compensated'):
        if hrtf_type == 'raw':
            self._hrtf_type_char = 'R'
        elif hrtf_type == 'compensated':
            self._hrtf_type_char = 'C'
        elif hrtf_type == 'compensated-interpolated':
            self._hrtf_type_char = 'I'
        else:
            raise ValueError(f'Unknown HRTF type "{hrtf_type}"')
        if samplerate not in (44100, 48000, 96000) or hrtf_type != 'compensated-interpolated':
            samplerate = 96000
        self._samplerate = samplerate
        super().__init__(collection_id='bili', sofa_directory_path=sofa_directory_path, variant_key=f'{hrtf_type}/{samplerate}')
        self._default_hrirs_exclude = () # TODO: Neumann KU100 and Brüel & Kjaer type 4100D with and without pinna dummies


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[1]) for x in (self.sofa_directory_path / self._variant_key).glob(f'IRC_????_{self._hrtf_type_char}_HRIR_{self._samplerate}.sofa')])


class ItaDataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path=None, anthropometry_csvfile_path=None):
        super().__init__(collection_id='ita', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_csvfile_path)
        self._default_hrirs_exclude = (2, 14) # lower resolution of measurement grid


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('MRT')[1]) for x in self.sofa_directory_path.glob('MRT??.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # mm
        self._anthropometry = {'sex': [], 'head-torso': [], 'pinna-size': {'left': [], 'right': []}}
        xlsx_file = load_workbook(anthropometry_path, read_only=True)
        worksheet = xlsx_file['Tabelle1']
        self._anthropometric_ids = np.array([row[0].value for row in worksheet['A2':'A49']])
        sex = np.array([[cell.value for cell in row] for row in worksheet['B2':'B49']])
        self._anthropometry['sex'] = np.select([sex == 'm', sex == 'w'], [0, 1], default=np.nan)
        self._anthropometry['head-torso'] = [[cell.value for cell in row] for row in worksheet['C2':'H49']]
        self._anthropometry['pinna-size']['left'] = [[cell.value for cell in row] for row in worksheet['I2':'P49']]
        self._anthropometry['pinna-size']['right'] = np.full_like(self._anthropometry['pinna-size']['left'], np.nan)
        xlsx_file.close()


    @property
    def _anthropometry_names(self):
        return {
            'sex': _CIPIC_ANTHROPOMETRY_NAMES['sex'],
            'head-torso': ('head width', 'head depth (front)', 'head depth (back)', 'mean head depth', 'pinna offset', 'head height'),
            'pinna-size': _CIPIC_ANTHROPOMETRY_NAMES['pinna-size'],
        }


class HutubsDataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path=None, anthropometry_csvfile_path=None, measured_hrtf=True):
        super().__init__(collection_id='hutubs', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_csvfile_path, variant_key='measured' if measured_hrtf else 'simulated')
        self._default_hrirs_exclude = (1, 96) # FABIAN dummy
        self._default_anthropometry_exclude = (1, 96) # FABIAN dummy
        self._default_mesh_exclude = (1, 96) # FABIAN dummy


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[0].split('pp')[1]) for x in self.sofa_directory_path.glob(f'pp??_HRIRs_{self._variant_key}.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # cm & deg
        self._anthropometry = {'head-torso': [], 'pinna-size': {'left': [], 'right': []}, 'pinna-angle': {'left': [], 'right': []}}
        anthropometry_ids = []
        with open(anthropometry_path, 'r') as f:
            f.readline()
            csv_file = csv.reader(f, quoting=csv.QUOTE_NONNUMERIC)
            for row in csv_file:
                if not np.isnan(row[1:]).all():
                    anthropometry_ids.append(int(row[0]))
                    self._anthropometry['head-torso'].append(10*np.array(row[1:14]))
                    self._anthropometry['pinna-size']['left'].append(10*np.array(row[14:24]))
                    self._anthropometry['pinna-angle']['left'].append(np.array(row[24:26]))
                    self._anthropometry['pinna-size']['right'].append(10*np.array(row[26:36]))
                    self._anthropometry['pinna-angle']['right'].append(np.array(row[36:38]))
        self._anthropometric_ids = np.array(anthropometry_ids)


    @property
    def _anthropometry_names(self):
        return {
            'head-torso': _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][:9] + _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][11:12] + _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][13:14] + _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][15:],
            'pinna-size': _CIPIC_ANTHROPOMETRY_NAMES['pinna-size'][:-1] + (_CIPIC_ANTHROPOMETRY_NAMES['pinna-size'][-1]+' (down)',) + ('cavum concha depth (back)', 'crus of helix depth'),
            'pinna-angle': _CIPIC_ANTHROPOMETRY_NAMES['pinna-angle'],
        }


class RiecDataQuery(HrirDataQuery):

    def __init__(self, sofa_directory_path):
        super().__init__(collection_id='riec', sofa_directory_path=sofa_directory_path)
        self._default_hrirs_exclude = (46, 80) # SAMRAI & KEMAR dummy
        self._default_mesh_exclude = (46,) # SAMRAI dummy


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[3]) for x in self.sofa_directory_path.glob('RIEC_hrir_subject_???.sofa')])


class ChedarDataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path=None, anthropometry_matfile_path=None, radius=1):
        if np.isclose(radius, 0.2):
            self._radius = '02m'
        elif np.isclose(radius, 0.5):
            self._radius = '05m'
        elif np.isclose(radius, 1):
            self._radius = '1m'
        elif np.isclose(radius, 2):
            self._radius = '2m'
        else:
            raise ValueError('The radius needs to be one of 0.2, 0.5, 1 or 2')
        super().__init__(collection_id='chedar', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_matfile_path, variant_key=self._radius)


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[1]) for x in self.sofa_directory_path.glob(f'chedar_????_UV{self._radius}.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # mm & deg
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            mat_data = read_mat(anthropometry_path, variable_names=['#subsystem#'])
        row_names = mat_data['#subsystem#']['MCOS'][5]
        self._anthropometric_ids = np.array([int(n.split('chedar_')[1]) for n in row_names])
        table_data = np.array(mat_data['#subsystem#']['MCOS'][11]).T
        pinna_size = np.column_stack((table_data[:, :8], table_data[:, 28:30]))
        self._anthropometry = {'head-torso': table_data[:, 10:28], 'pinna-size': {'left': pinna_size, 'right': pinna_size}, 'pinna-angle': {'left': table_data[:, 8:10], 'right': table_data[:, 8:10]}}


    @property
    def _anthropometry_names(self):
        return {
            'head-torso': ('x1', 'x11a', 'x11b', 'x12', 'x12a', 'x12b', 'x13', 'x2', 'x2a', 'x2b', 'x3', 'x3a', 'x3b', 'x4', 'x5', 'x6', 'x7', 'x8'),
            'pinna-size': ('d1', 'd1d2', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7', 'P', 'R'),
            'pinna-angle': ('t1', 't2'),
        }


class WidespreadDataQuery(HrirDataQuery):

    def __init__(self, sofa_directory_path='', radius=1, grid='UV'):
        if np.isclose(radius, 0.2):
            self._radius = '02m'
        elif np.isclose(radius, 0.5):
            self._radius = '05m'
        elif np.isclose(radius, 1):
            self._radius = '1m'
        elif np.isclose(radius, 2):
            self._radius = '2m'
        else:
            raise ValueError('The radius needs to be one of 0.2, 0.5, 1 or 2')
        if grid not in ('UV', 'ICO'):
            raise ValueError('The grid needs to be either "UV" or "ICO".')
        self._grid = grid
        super().__init__(collection_id='widespread', sofa_directory_path=sofa_directory_path, variant_key=f'{self._grid}-{self._radius}')


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[1]) for x in self.sofa_directory_path.glob(f'{self._grid}{self._radius}_?????.sofa')])


class Sadie2DataQuery(HrirDataQuery, ImageDataQuery):

    def __init__(self, sofa_directory_path=None, image_directory_path=None, samplerate=96000):
        if samplerate == 44100:
            self._samplerate_str = '44K_16bit_256tap'
        elif samplerate == 48000:
            self._samplerate_str = '48K_24bit_256tap'
        else:
            self._samplerate_str = '96K_24bit_512tap'
        super().__init__(collection_id='sadie2', sofa_directory_path=sofa_directory_path, image_directory_path=image_directory_path, variant_key=f'{samplerate}')
        self._default_hrirs_exclude = (1, 2, 3, 4, 5, 6, 7, 8, 9) # higher spatial resolution
        self._default_images_exclude = (1, 2, 3, 16) # dummies (1, 2) & empty images (3, 16)


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[0][1:]) for x in self.sofa_directory_path.glob(f'[DH]*/[DH]*_HRIR_SOFA/[DH]*_{self._samplerate_str}_FIR_SOFA.sofa')])


    def _all_image_ids(self, side, rear):
        if rear:
            raise ValueError('No rear pictures available in the SADIE II dataset')
        side_str = self._image_side_str(side)
        return sorted([int(x.stem.split('_')[0].split()[0][1:]) for x in self.image_directory_path.glob(f'[DH]*/[DH]*_Scans/[DH]*[_ ]{side_str}.png')])


    @staticmethod
    def _image_side_str(side):
        return '({})'.format(side.split('-')[-1][0].upper())


class ThreeDThreeADataQuery(HrirDataQuery, AnthropometryDataQuery):

    def __init__(self, sofa_directory_path='', anthropometry_directory_path='', hrtf_method='measured', hrtf_type='compensated'):
        if hrtf_type == 'raw':
            self._hrtf_type_str = 'BIRs'
        elif hrtf_type == 'compensated':
            self._hrtf_type_str = 'HRIRs'
        elif hrtf_type == 'compensated-lowfreqextended':
            self._hrtf_type_str = 'HRIRs_lfc'
        elif hrtf_type == 'compensated-equalized':
            self._hrtf_type_str = 'HRIRs_dfeq'
        else:
            raise ValueError(f'Unknown HRTF type "{hrtf_type}"')
        if hrtf_method == 'measured':
            self._method_str = 'Acoustic'
        else:
            if hrtf_type not in ('compensated', 'compensated-equalized'):
                raise ValueError('Only compensated and diffuse field equalized types of HRTF available for BEM-simulations')
            if hrtf_method == 'simulated-head':
                self._method_str = 'BEM/Head-Only'
            elif hrtf_method == 'simulated-head_ears':
                self._method_str = 'BEM/Head-and-Ears'
            elif hrtf_method == 'simulated-head_ears_torso-consumer_grade':
                self._method_str = 'BEM/Head-Ears-and-Torso/Consumer-Grade'
            elif hrtf_method == 'simulated-head_ears_torso-reference_grade':
                self._method_str = 'BEM/Head-Ears-and-Torso/Reference-Grade'
            else:
                raise ValueError(f'Unknown HRTF method "{hrtf_method}"')
        super().__init__(collection_id='3d3a', sofa_directory_path=sofa_directory_path, anthropometry_path=anthropometry_directory_path, variant_key=f'{hrtf_method}-{hrtf_type}')
        self._default_hrirs_exclude = (37, 44) # Neumann KU100 and Brüel & Kjaer HATS 4128C dummy
        self._default_anthropometry_exclude = (37, 44) # Neumann KU100 and Brüel & Kjaer HATS 4128C dummy


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[0].lstrip('Subject')) for x in self.sofa_directory_path.glob(f'{self._method_str}/Subject*/Subject*_{self._hrtf_type_str}.sofa')])


    def _load_anthropometry(self, anthropometry_path):
        # m
        self._anthropometry = {'head-torso': [], 'pinna-size': {'left': [], 'right': []}}
        anthropometry_ids = []
        for mat_path in sorted(anthropometry_path.glob('Subject*.mat')):
            mat_anth = io.loadmat(mat_path, squeeze_me=True)
            anthropometry_ids.append(int(mat_anth['subjectID'].split('Subject')[-1]))
            head_torso = 1000*np.array([mat_anth['headWidth'], mat_anth['headHeight'], mat_anth['headDepth']])
            self._anthropometry['head-torso'].append(head_torso)
            self._anthropometry['pinna-size']['left'].append(1000*np.array([mat_anth['pinnaFlareL']]))
            self._anthropometry['pinna-size']['right'].append(1000*np.array([mat_anth['pinnaFlareR']]))
        self._anthropometric_ids = np.array(anthropometry_ids)


    @property
    def _anthropometry_names(self):
        return {
            'head-torso': _CIPIC_ANTHROPOMETRY_NAMES['head-torso'][:3],
            'pinna-size': ('pinna flare distance',),
        }


class SonicomDataQuery(HrirDataQuery):

    def __init__(self, sofa_directory_path='', samplerate=96000, hrtf_type='compensated'):
        if samplerate not in (44100, 48000, 96000):
            samplerate = 96000
        self._samplerate_str = f'{round(samplerate/1000)}kHz'
        if hrtf_type == 'raw':
            self._hrtf_type_str = 'Raw'
        elif hrtf_type == 'raw-nodelay':
            self._hrtf_type_str = 'Raw_NoITD'
        elif hrtf_type == 'windowed':
            self._hrtf_type_str = 'Windowed'
        elif hrtf_type == 'windowed-nodelay':
            self._hrtf_type_str = 'Windowed_NoITD'
        elif hrtf_type == 'compensated':
            self._hrtf_type_str = 'FreeFieldComp'
        elif hrtf_type == 'compensated-nodelay':
            self._hrtf_type_str = 'FreeFieldComp_NoITD'
        elif hrtf_type == 'compensated-minphase':
            self._hrtf_type_str = 'FreeFieldCompMinPhase'
        elif hrtf_type == 'compensated-minphase-nodelay':
            self._hrtf_type_str = 'FreeFieldCompMinPhase_NoITD'
        else:
            raise ValueError(f'Unknown HRTF type "{hrtf_type}"')
        super().__init__(collection_id='sonicom', sofa_directory_path=sofa_directory_path, variant_key=f'{hrtf_type}-{samplerate}')


    def _all_hrir_ids(self, side):
        return sorted([int(x.stem.split('_')[0].lstrip('P')) for x in self.sofa_directory_path.glob(f'P????/HRTF/{self._samplerate_str}/P????_{self._hrtf_type_str}_{self._samplerate_str}.sofa')])
