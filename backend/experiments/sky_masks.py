"""External sky masks for an opt-in, local experimental input route.

The frozen helpers are executed unchanged with a module-local Image facade that
adds validated sky labels to source alpha in memory. No source image, baseline,
old scene, SfM result or budget is modified. This module starts no child process.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys
from types import ModuleType, SimpleNamespace

FROZEN_SHA = {
    'scripts/prepare_dataset.py': '96e21987a8fc8d62b7ad7de02e0a337b21377293c794144e37b53d94ed493363',
    'scripts/prepare_openmvs_texture_input.py': '57d0a5c22c74b3ff43365f1cc6dea6a64e6269003c68aededec6547f6f0f185c',
}
ALLOWED_WHITE = frozenset({'00057.jpg', '00302.jpg'})
SUPPORTED = frozenset({'.jpg', '.jpeg', '.png'})


class SkyMaskError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise SkyMaskError(code)


def ordinary(path, *, missing=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts
            and not str(path).startswith(('\\\\', '//'))
            and not any(':' in p for p in path.parts[1:]), 'SKY_PATH_INVALID')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            require(missing and current == path, 'SKY_PATH_MISSING')
            return path
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 1024,
                'SKY_REPARSE_REJECTED')
    return path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    with ordinary(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path, expected=None):
    path = ordinary(path)
    require(path.is_file() and path.stat().st_size <= 16 * 1024**2, 'SKY_DOCUMENT_INVALID')
    raw = path.read_bytes()
    require(expected is None or digest(raw) == expected, 'SKY_DOCUMENT_HASH_CHANGED')
    def unique(items):
        out = {}
        for key, value in items:
            require(key not in out, 'SKY_DUPLICATE_KEY')
            out[key] = value
        return out
    try:
        doc = json.loads(raw, object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError) as error:
        if isinstance(error, SkyMaskError):
            raise
        raise SkyMaskError('SKY_DOCUMENT_INVALID') from None
    require(type(doc) is dict, 'SKY_DOCUMENT_INVALID')
    return doc


def write_new(path, doc):
    path = ordinary(path, missing=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(doc, stream, indent=2, allow_nan=False)
        stream.write('\n')


def _stat(path):
    info = ordinary(path).stat()
    return {'size_bytes': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _mask(path, expected_hash, size):
    from PIL import Image
    path = ordinary(path)
    require(path.stat().st_size <= 8 * 1024**2, 'SKY_MASK_TOO_LARGE')
    raw = path.read_bytes()
    require(digest(raw) == expected_hash, 'SKY_MASK_HASH_CHANGED')
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        require(image.mode == 'L' and image.size == size, 'SKY_MASK_MODE_OR_SIZE')
        histogram = image.histogram()
        require({i for i, count in enumerate(histogram) if count} <= {0, 255}
                and histogram[255] > 0, 'SKY_MASK_NOT_BINARY_OR_EMPTY')
        return image.copy()


def inspect_masks(source: Path, mask_root: Path, *, expected_manifest_sha256: str,
                  expected_count=300, guard=lambda: None):
    """Read actual source/mask bytes once; never create a scene or claim budget."""
    from PIL import Image
    source, mask_root = ordinary(source), ordinary(mask_root)
    manifest = read_json(mask_root / 'manifest.json', expected_manifest_sha256)
    accepted = read_json(mask_root / 'ACCEPTED.json')
    require(accepted.get('status') == 'accepted_conservative_label_assets_only'
            and accepted.get('manifest_sha256') == expected_manifest_sha256, 'SKY_ASSETS_NOT_ACCEPTED')
    selection_sha = manifest['selection_manifest_sha256']
    selection = read_json(source / 'selection-manifest.json', selection_sha)
    rows = manifest['files']; names = [row['filename'] for row in rows]
    require(type(expected_count) is int and expected_count >= 3 and len(rows) == expected_count
            and len(set(names)) == expected_count, 'SKY_COUNT_OR_DUPLICATE')
    require(all(isinstance(n, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', n)
                and Path(n).name == n and Path(n).suffix.lower() in SUPPORTED for n in names),
            'SKY_FILENAME_INVALID')
    require(len({Path(n).stem.casefold() for n in names}) == expected_count, 'SKY_STEM_COLLISION')
    require(names == [r['filename'] for r in selection['files']], 'SKY_SELECTION_ORDER_CHANGED')
    require(set(names) == {p.name for p in source.iterdir() if p.suffix.lower() in SUPPORTED},
            'SKY_SOURCE_SET_CHANGED')
    for folder, wanted in (
        ('colmap', {n + '.png' for n in names}),
        ('openmvs', {Path(n).stem + '.mask.png' for n in names}),
    ):
        directory = ordinary(mask_root / 'masks' / folder)
        require({p.name for p in directory.iterdir()} == wanted, 'SKY_MASK_SET_CHANGED')
    allowed = accepted.get('label_checks', {}).get('all_white_exceptions', {})
    require(type(allowed) is dict and set(allowed) <= ALLOWED_WHITE, 'SKY_WHITE_EXCEPTION_INVALID')
    selected = {row['filename']: row for row in selection['files']}
    bound, whites, sizes = [], [], set()
    for row in rows:
        guard(); name = row['filename']; before = _stat(source / name)
        expected_source = selected[name]['sha256']
        require(before['size_bytes'] == selected[name]['size_bytes']
                and file_sha(source / name) == expected_source, 'SKY_SOURCE_HASH_CHANGED')
        require(row['source_sha256_before'] == row['source_sha256_after'] == expected_source,
                'SKY_SOURCE_PROVENANCE_CHANGED')
        with Image.open(source / name) as image:
            size = image.size
            require(image.getexif().get(274, 1) == 1, 'SKY_EXIF_ORIENTATION')
        sizes.add(size)
        require(size == (row['width'], row['height']), 'SKY_SOURCE_SIZE_CHANGED')
        pixels = []
        for kind, rel in (('colmap_mask', 'masks/colmap/' + name + '.png'),
                          ('openmvs_mask', 'masks/openmvs/' + Path(name).stem + '.mask.png')):
            info = row['files'][kind]
            require(info['path'] == rel, 'SKY_MASK_PATH_CHANGED')
            require(_stat(mask_root / rel)['size_bytes'] == info['size_bytes'], 'SKY_MASK_SIZE_CHANGED')
            mask = _mask(mask_root / rel, info['sha256'], size)
            pixels.append(mask.tobytes())
        require(pixels[0] == pixels[1], 'SKY_MASK_PAIR_CHANGED')
        if not mask.histogram()[0]:
            require(name in allowed, 'SKY_UNEXPECTED_ALL_WHITE')
            whites.append(name)
        require(_stat(source / name) == before, 'SKY_SOURCE_CHANGED_DURING_READ')
        bound.append({'filename': name, 'width': size[0], 'height': size[1],
            'source_sha256': expected_source, 'source_stat': before,
            'pixel_sha256': digest(pixels[0]), 'colmap_sha256': row['files']['colmap_mask']['sha256'],
            'openmvs_sha256': row['files']['openmvs_mask']['sha256']})
    require(len(sizes) == 1 and set(whites) == set(allowed), 'SKY_DIMENSIONS_OR_WHITE_SET_CHANGED')
    return {'schema_version': '1.0', 'kind': 'local_sky_mask_experimental_binding',
        'source_root': str(source), 'mask_root': str(mask_root),
        'manifest_sha256': expected_manifest_sha256, 'selection_manifest_sha256': selection_sha,
        'images': bound, 'all_white_exceptions': whites,
        'execution_authorized': False, 'budget_claimed': False, 'budget_charged': False,
        'reconstruction_started': False, 'server_accessed': False, 'uac_requested': False}


def _new_output(output, protected):
    output = ordinary(output, missing=True)
    require(not output.exists(), 'SKY_NEW_OUTPUT_REQUIRED')
    require(all(not output.is_relative_to(p) and not p.is_relative_to(output) for p in protected),
            'SKY_SOURCE_OUTPUT_OVERLAP')
    output.mkdir()
    return output


def _load_helper(pipeline_root, name):
    rel = 'scripts/' + name
    path = ordinary(Path(pipeline_root) / rel)
    raw = path.read_bytes()
    require(digest(raw) == FROZEN_SHA[rel], 'SKY_FROZEN_HELPER_CHANGED')
    module = ModuleType('re3d_frozen_sky_prepare')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def _run_helper(binding, module, images, argv, output, guard):
    from PIL import Image, ImageChops
    image_root, mask_root = ordinary(images), ordinary(Path(binding['mask_root']))
    require(file_sha(mask_root / 'manifest.json') == binding['manifest_sha256'], 'SKY_BOUND_MANIFEST_CHANGED')
    expected = {r['filename']: r for r in binding['images']}
    require({p.name for p in image_root.iterdir() if p.suffix.lower() in SUPPORTED} == set(expected),
            'SKY_PREPARE_IMAGE_SET_CHANGED')
    observed = {}; combined = {}
    def open_masked(path, *args, **kwargs):
        guard(); path = ordinary(Path(path)); row = expected.get(path.name)
        require(path.parent == image_root and row is not None, 'SKY_UNEXPECTED_IMAGE_READ')
        require(file_sha(path) == row['source_sha256'], 'SKY_PREPARE_SOURCE_CHANGED')
        require(path.name not in observed, 'SKY_DUPLICATE_IMAGE_READ')
        with Image.open(path, *args, **kwargs) as image:
            image.load(); rgba = image.convert('RGBA')
        mask_name = row.get('mask_filename', path.name)
        require(isinstance(mask_name, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', mask_name)
                and Path(mask_name).name == mask_name, 'SKY_MASK_ALIAS_INVALID')
        sky = _mask(mask_root / 'masks' / 'colmap' / (mask_name + '.png'),
                    row['colmap_sha256'], (row['width'], row['height']))
        require(digest(sky.tobytes()) == row['pixel_sha256'], 'SKY_BOUND_PIXELS_CHANGED')
        alpha = rgba.getchannel('A').point(lambda x: 255 if x >= 128 else 0)
        keep = ImageChops.darker(alpha, sky)
        require(keep.histogram()[255] > 0, 'SKY_EMPTY_COMBINED_FOREGROUND')
        rgba.putalpha(keep)
        observed[path.name] = row['pixel_sha256']; combined[path.name] = digest(keep.tobytes())
        return rgba
    module.Image = SimpleNamespace(open=open_masked, fromarray=Image.fromarray, new=Image.new)
    previous = sys.argv
    try:
        sys.argv = [module.__file__, *map(str, argv)]
        with redirect_stdout(io.StringIO()):
            module.main()
    finally:
        sys.argv = previous
    require(set(observed) == set(expected), 'SKY_INCOMPLETE_MASK_CONSUMPTION')
    records = []
    dataset = '--source' in argv
    for name, row in expected.items():
        guard()
        rel = 'masks/' + name + '.png' if dataset else 'images/' + Path(name).stem + '.mask.png'
        raw = ordinary(output / rel).read_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            require(image.mode == 'L' and image.size == (row['width'], row['height'])
                    and digest(image.tobytes()) == combined[name], 'SKY_PREPARED_MASK_MISMATCH')
        if dataset:
            require(file_sha(output / 'images' / name) == row['source_sha256'], 'SKY_COPIED_IMAGE_CHANGED')
        require(file_sha(image_root / name) == row['source_sha256'], 'SKY_INPUT_CHANGED_BY_PREPARATION')
        records.append({'filename': name, 'mask': rel, 'mask_sha256': digest(raw),
                        'external_pixel_sha256': observed[name], 'effective_pixel_sha256': combined[name]})
    receipt = {'status': 'frozen_preparation_consumed_external_masks',
        'frozen_helper_sha256': FROZEN_SHA['scripts/' + Path(module.__file__).name],
        'source_mask_manifest_sha256': binding['manifest_sha256'], 'consumed_count': len(observed),
        'method': 'module-local Image facade; effective alpha = source alpha AND external sky keep; frozen helper bytes unchanged',
        'source_written': False, 'reconstruction_started': False, 'files': records}
    write_new(output / 'sky-mask-consumption.json', receipt)
    return receipt


def prepare_frozen_dataset(binding, *, pipeline_root: Path, output: Path, guard=lambda: None):
    source, masks, pipeline = (ordinary(Path(binding['source_root'])),
                               ordinary(Path(binding['mask_root'])), ordinary(pipeline_root))
    module = _load_helper(pipeline, 'prepare_dataset.py')
    guard(); output = _new_output(output, (source, masks, pipeline))
    return _run_helper(binding, module, source,
        ['--source', source, '--output', output, '--alpha-threshold', '128'], output, guard)


def prepare_frozen_openmvs(binding, *, pipeline_root: Path, images: Path, sparse: Path,
                          output: Path, guard=lambda: None):
    source, masks, pipeline, sparse, images = (ordinary(Path(binding['source_root'])),
        ordinary(Path(binding['mask_root'])), ordinary(pipeline_root), ordinary(sparse), ordinary(images))
    module = _load_helper(pipeline, 'prepare_openmvs_texture_input.py')
    guard(); output = _new_output(output, (source, masks, pipeline, sparse, images))
    return _run_helper(binding, module, images,
        ['--images', images, '--sparse', sparse, '--output', output, '--alpha-threshold', '128'], output, guard)
