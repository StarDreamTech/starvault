#!/usr/bin/env python3
"""Compress rendered, fixed promotional artwork and refresh its catalog.

Pillow is only an asset-making dependency; the desktop app embeds the final PNGs.
Run after: swift tools/render_share.swift assets
"""
import hashlib
import json
from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[1]
directory = root / 'assets/share'
path = directory / 'catalog.json'
catalog = json.loads(path.read_text(encoding='utf-8'))
originals = root / '.local/share-poster-originals'
originals.mkdir(parents=True, exist_ok=True)
for item in catalog['images']:
    image_path = directory / (item['id'] + '.png')
    original = image_path.read_bytes()
    (originals / image_path.name).write_bytes(original)
    with Image.open(image_path) as image:
        assert image.size == (item['width'], item['height'])
        image.convert('RGB').quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                     dither=Image.Dither.NONE).save(image_path, optimize=True)
    data = image_path.read_bytes()
    item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    print(item['id'], len(data))
path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
