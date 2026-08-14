# -*- coding: utf-8 -*-
"""gifwriter - a tiny, dependency-free animated GIF89a encoder.

Built for Glass so the editor's sprite animator can export GIFs with NO external
libraries (no Pillow / numpy). Each frame gets its own <=256 colour palette via
median-cut, fully-transparent pixels become a transparent index, and frames are
LZW-compressed per the GIF spec.

    write_gif(path, frames, delays_ms, loop=0)
        frames    : list of (width, height, rgba_bytes)  (rgba_bytes = w*h*4 bytes)
        delays_ms : list of per-frame delays in milliseconds (or a single int)
        loop      : 0 = loop forever, n = loop n times
"""

import struct


# ---------------------------------------------------------------- median cut
def _median_cut(pixels, want):
    """pixels: list of (r,g,b). Return up to `want` representative colours."""
    if not pixels:
        return [(0, 0, 0)]
    boxes = [pixels]
    while len(boxes) < want:
        # pick the box with the largest colour spread to split
        best_i, best_range = -1, -1
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            rng = _box_range(box)
            if rng[0] > best_range:
                best_range, best_i, best_axis = rng[0], i, rng[1]
        if best_i < 0:
            break
        box = boxes.pop(best_i)
        box.sort(key=lambda c: c[best_axis])
        mid = len(box) // 2
        boxes.append(box[:mid])
        boxes.append(box[mid:])
    palette = []
    for box in boxes:
        n = len(box)
        if not n:
            continue
        r = sum(c[0] for c in box) // n
        g = sum(c[1] for c in box) // n
        b = sum(c[2] for c in box) // n
        palette.append((r, g, b))
    return palette or [(0, 0, 0)]


def _box_range(box):
    lo = [255, 255, 255]
    hi = [0, 0, 0]
    for c in box:
        for a in range(3):
            if c[a] < lo[a]:
                lo[a] = c[a]
            if c[a] > hi[a]:
                hi[a] = c[a]
    spans = [hi[a] - lo[a] for a in range(3)]
    axis = spans.index(max(spans))
    return spans[axis], axis


def _nearest(palette, c, cache):
    hit = cache.get(c)
    if hit is not None:
        return hit
    best_i, best_d = 0, 1 << 30
    for i, p in enumerate(palette):
        d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2
        if d < best_d:
            best_d, best_i = d, i
            if d == 0:
                break
    cache[c] = best_i
    return best_i


# ---------------------------------------------------------------- LZW (GIF)
def _lzw_encode(indices, min_code_size):
    clear = 1 << min_code_size
    end = clear + 1
    code_size = min_code_size + 1
    dict_, next_code = {}, end + 1

    out = bytearray()
    cur, ncur = 0, 0                       # bit accumulator

    def emit(code):
        nonlocal cur, ncur
        cur |= code << ncur
        ncur += code_size
        while ncur >= 8:
            out.append(cur & 0xFF)
            cur >>= 8
            ncur -= 8

    def reset_dict():
        nonlocal dict_, next_code, code_size
        dict_ = {}
        next_code = end + 1
        code_size = min_code_size + 1

    emit(clear)
    if not indices:
        emit(end)
        if ncur > 0:
            out.append(cur & 0xFF)
        return bytes(out)

    prev = indices[0]
    for k in indices[1:]:
        key = (prev, k)
        if key in dict_:
            prev = dict_[key]
        else:
            emit(prev)
            dict_[key] = next_code
            next_code += 1
            if next_code > (1 << code_size) and code_size < 12:
                code_size += 1
            if next_code >= 4096:
                emit(clear)
                reset_dict()
            prev = k
    emit(prev)
    emit(end)
    if ncur > 0:
        out.append(cur & 0xFF)
    return bytes(out)


def _blockify(data):
    out = bytearray()
    i = 0
    while i < len(data):
        chunk = data[i:i + 255]
        out.append(len(chunk))
        out.extend(chunk)
        i += 255
    out.append(0)                          # block terminator
    return bytes(out)


# ---------------------------------------------------------------- frame -> indexed
def _index_frame(w, h, rgba, max_colors=255):
    """Return (indices, palette, transparent_index or None)."""
    n = w * h
    has_alpha = False
    opaque = []
    px = []                                 # per-pixel (r,g,b) or None if transparent
    for i in range(n):
        a = rgba[i * 4 + 3]
        if a < 128:
            px.append(None)
            has_alpha = True
        else:
            c = (rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
            px.append(c)
            opaque.append(c)

    # unique opaque colours; quantise only if there are too many
    uniq = list(dict.fromkeys(opaque))
    if len(uniq) <= max_colors:
        palette = uniq or [(0, 0, 0)]
    else:
        palette = _median_cut(list(opaque), max_colors)

    trans_index = None
    if has_alpha:
        trans_index = len(palette)          # reserve one slot for transparency
        palette = list(palette) + [(0, 0, 0)]

    cache = {}
    lut = {c: i for i, c in enumerate(palette)}
    indices = bytearray(n)
    for i, c in enumerate(px):
        if c is None:
            indices[i] = trans_index if trans_index is not None else 0
        else:
            hit = lut.get(c)
            indices[i] = hit if hit is not None else _nearest(palette, c, cache)
    return indices, palette, trans_index


def _pad_palette(palette):
    """Pad to a power-of-two size (2..256). Return (bytes, color_bits)."""
    size = len(palette)
    p2 = 2
    bits = 1
    while p2 < size:
        p2 <<= 1
        bits += 1
    data = bytearray()
    for c in palette:
        data += bytes((c[0], c[1], c[2]))
    for _ in range(p2 - size):
        data += b"\x00\x00\x00"
    return bytes(data), bits


# ---------------------------------------------------------------- public API
def write_gif(path, frames, delays_ms, loop=0):
    if not frames:
        raise ValueError("no frames")
    if isinstance(delays_ms, int):
        delays_ms = [delays_ms] * len(frames)
    W = max(f[0] for f in frames)
    H = max(f[1] for f in frames)

    out = bytearray()
    out += b"GIF89a"
    out += struct.pack("<HH", W, H)
    out += bytes((0x70, 0, 0))              # no global table; colour resolution
    # NETSCAPE looping extension
    out += b"\x21\xFF\x0B" + b"NETSCAPE2.0" + b"\x03\x01" + struct.pack("<H", loop) + b"\x00"

    for idx, (w, h, rgba) in enumerate(frames):
        indices, palette, trans = _index_frame(w, h, rgba)
        pal_bytes, bits = _pad_palette(palette)
        delay_cs = max(2, int(round(delays_ms[idx] / 10.0)))   # GIF delay is 1/100s

        # Graphic Control Extension (delay + transparency)
        packed = 0x01 if trans is not None else 0x00           # transparent flag
        packed |= (2 << 2)                                     # disposal = restore bg
        out += b"\x21\xF9\x04" + bytes((packed,)) + struct.pack("<H", delay_cs)
        out += bytes((trans if trans is not None else 0,)) + b"\x00"

        # Image Descriptor (with a local colour table)
        out += b"\x2C" + struct.pack("<HHHH", 0, 0, w, h)
        out += bytes((0x80 | (bits - 1),))                     # local table, size
        out += pal_bytes

        min_code = max(2, bits)
        out += bytes((min_code,))
        out += _blockify(_lzw_encode(list(indices), min_code))

    out += b"\x3B"                                             # trailer
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path
