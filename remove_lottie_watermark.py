#!/usr/bin/env python3
"""
remove_lottie_watermark.py
Remove the corner watermark/logo from Lottie (or Jitter-exported) JSON files.

Usage:
    python remove_lottie_watermark.py input.json                  # -> input_no_watermark.json
    python remove_lottie_watermark.py a.json b.json c.json        # batch
    python remove_lottie_watermark.py folder/                     # every .json in folder
    python remove_lottie_watermark.py input.json -o clean.json    # custom output (single file)
    python remove_lottie_watermark.py input.json --dry-run        # just report what it finds
    python remove_lottie_watermark.py input.json --corner bottom-left
    python remove_lottie_watermark.py input.json --layer 12       # force-remove layer by 'ind'

How it works:
    1. Looks at top-level precomp/image/shape layers and works out where each one
       sits on the canvas (walking up its parent chain of null layers).
    2. A layer is flagged as a watermark if it is small, sits in the chosen corner
       (default bottom-right) and is NOT animated into the main content.
    3. Removes the layer, its now-childless parent null layers, and any assets
       (precomps/images) that nothing references anymore.
"""
import argparse
import json
import os
import sys

CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


# --------------------------------------------------------------------------- helpers
def static_value(prop, default):
    """Return the static value of a Lottie property, or the first keyframe value."""
    if not isinstance(prop, dict) or "k" not in prop:
        return default
    k = prop["k"]
    if isinstance(k, list) and k and isinstance(k[0], dict):  # keyframed
        s = k[0].get("s")
        return s if s is not None else default
    return k if k is not None else default


def is_animated(prop):
    k = prop.get("k") if isinstance(prop, dict) else None
    return isinstance(k, list) and bool(k) and isinstance(k[0], dict)


def layer_transform(layer):
    ks = layer.get("ks", {}) or {}
    pos = static_value(ks.get("p"), [0, 0])
    anc = static_value(ks.get("a"), [0, 0])
    sca = static_value(ks.get("s"), [100, 100])
    pos = (list(pos) + [0, 0])[:2]
    anc = (list(anc) + [0, 0])[:2]
    sca = (list(sca) + [100, 100])[:2]
    return pos, anc, sca


def absolute_box(layer, by_ind):
    """Approximate (x0, y0, x1, y1) of a layer on the canvas using static transforms."""
    w = layer.get("w") or layer.get("sw") or 0
    h = layer.get("h") or layer.get("sh") or 0
    pos, anc, sca = layer_transform(layer)

    sx, sy = sca[0] / 100.0, sca[1] / 100.0
    x0 = pos[0] - anc[0] * sx
    y0 = pos[1] - anc[1] * sy
    x1, y1 = x0 + w * sx, y0 + h * sy

    # walk parents (null layers etc.), applying translation + scale
    seen = set()
    parent = layer.get("parent")
    while parent is not None and parent in by_ind and parent not in seen:
        seen.add(parent)
        p = by_ind[parent]
        ppos, panc, psca = layer_transform(p)
        psx, psy = psca[0] / 100.0, psca[1] / 100.0
        ox = ppos[0] - panc[0] * psx
        oy = ppos[1] - panc[1] * psy
        x0, x1 = ox + x0 * psx, ox + x1 * psx
        y0, y1 = oy + y0 * psy, oy + y1 * psy
        parent = p.get("parent")
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def in_corner(box, W, H, corner):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    right = "right" in corner
    bottom = "bottom" in corner
    ok_x = cx > W * 0.5 if right else cx < W * 0.5
    ok_y = cy > H * 0.6 if bottom else cy < H * 0.4
    return ok_x and ok_y


def count_refs(obj, target):
    n = 0
    if isinstance(obj, dict):
        if obj.get("refId") == target:
            n += 1
        for v in obj.values():
            n += count_refs(v, target)
    elif isinstance(obj, list):
        for v in obj:
            n += count_refs(v, target)
    return n


def used_asset_ids(data):
    used = set()

    def walk(o):
        if isinstance(o, dict):
            if "refId" in o:
                used.add(o["refId"])
            for k, v in o.items():
                if k != "assets":
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data.get("layers", []))
    # assets referenced from other (used) assets
    changed = True
    while changed:
        changed = False
        for a in data.get("assets", []):
            if a.get("id") in used and "layers" in a:
                before = len(used)
                walk(a["layers"])
                if len(used) != before:
                    changed = True
    return used


# --------------------------------------------------------------------------- detection
def find_watermark_layers(data, corner="bottom-right", max_area=0.15):
    W, H = data.get("w", 0), data.get("h", 0)
    layers = data.get("layers", [])
    by_ind = {l["ind"]: l for l in layers if "ind" in l}
    canvas_area = float(W * H) or 1.0

    candidates = []
    for l in layers:
        if l.get("ty") not in (0, 2, 4, 5):  # precomp, image, shape, text
            continue
        box = absolute_box(l, by_ind)
        area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
        if area == 0 or area / canvas_area > max_area:
            continue
        if not in_corner(box, W, H, corner):
            continue
        # Skip layers that clearly animate (real content usually moves)
        ks = l.get("ks", {}) or {}
        if is_animated(ks.get("p", {})) or is_animated(ks.get("o", {})):
            continue
        # Skip layers whose parent chain is animated
        animated_parent = False
        p = l.get("parent")
        while p is not None and p in by_ind:
            pk = by_ind[p].get("ks", {}) or {}
            if is_animated(pk.get("p", {})):
                animated_parent = True
                break
            p = by_ind[p].get("parent")
        if animated_parent:
            continue
        candidates.append((l, box, area))

    # If several qualify, prefer the one that is a precomp referenced exactly once
    # and sits closest to the corner.
    def score(item):
        l, box, area = item
        s = 0
        if l.get("ty") == 0:
            s += 2
        if l.get("refId") and count_refs(data, l["refId"]) == 1:
            s += 2
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        dx = (W - cx) if "right" in corner else cx
        dy = (H - cy) if "bottom" in corner else cy
        s -= (dx + dy) / max(W + H, 1)
        return s

    candidates.sort(key=score, reverse=True)
    return [c[0] for c in candidates[:1]]  # only the single best match


# --------------------------------------------------------------------------- removal
def remove_layers(data, inds):
    layers = data["layers"]
    remove = set(inds)

    # also remove parent null layers that would be left with no children
    changed = True
    while changed:
        changed = False
        for l in layers:
            if l.get("ind") in remove:
                p = l.get("parent")
                if p is not None and p not in remove:
                    still_used = any(
                        o.get("parent") == p and o.get("ind") not in remove
                        for o in layers
                    )
                    parent_layer = next((o for o in layers if o.get("ind") == p), None)
                    if (
                        parent_layer is not None
                        and parent_layer.get("ty") == 3  # null
                        and not still_used
                    ):
                        remove.add(p)
                        changed = True

    data["layers"] = [l for l in layers if l.get("ind") not in remove]

    # drop orphaned assets
    used = used_asset_ids(data)
    before = len(data.get("assets", []))
    data["assets"] = [a for a in data.get("assets", []) if a.get("id") in used]
    return len(layers) - len(data["layers"]), before - len(data["assets"])


def process(path, out=None, corner="bottom-right", dry_run=False, force_layer=None):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "layers" not in data:
        print(f"[skip] {path}: not a Lottie JSON (no 'layers').")
        return False

    if force_layer is not None:
        targets = [l for l in data["layers"] if l.get("ind") == force_layer]
    else:
        targets = find_watermark_layers(data, corner)

    if not targets:
        print(f"[none] {path}: no watermark found in the {corner} corner.")
        return False

    t = targets[0]
    print(
        f"[found] {path}: layer ind={t.get('ind')} type={t.get('ty')} "
        f"refId={t.get('refId')} size={t.get('w')}x{t.get('h')} "
        f"(canvas {data.get('w')}x{data.get('h')})"
    )
    if dry_run:
        return True

    n_layers, n_assets = remove_layers(data, [t["ind"]])
    if out is None:
        base, ext = os.path.splitext(path)
        out = f"{base}_no_watermark{ext or '.json'}"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"[done]  removed {n_layers} layer(s), {n_assets} asset(s) -> {out}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Remove watermark from Lottie/Jitter JSON.")
    ap.add_argument("inputs", nargs="+", help="JSON file(s) or folder(s)")
    ap.add_argument("-o", "--output", help="output path (single input only)")
    ap.add_argument("--corner", choices=CORNERS, default="bottom-right")
    ap.add_argument("--layer", type=int, help="force-remove the layer with this 'ind'")
    ap.add_argument("--dry-run", action="store_true", help="only report, don't write")
    args = ap.parse_args()

    files = []
    for p in args.inputs:
        if os.path.isdir(p):
            files += [
                os.path.join(p, f)
                for f in sorted(os.listdir(p))
                if f.lower().endswith(".json") and "_no_watermark" not in f
            ]
        else:
            files.append(p)

    if args.output and len(files) != 1:
        sys.exit("--output can only be used with a single input file.")

    ok = 0
    for f in files:
        try:
            ok += bool(process(f, args.output, args.corner, args.dry_run, args.layer))
        except Exception as e:  # keep batch going
            print(f"[error] {f}: {e}")
    print(f"\n{ok}/{len(files)} file(s) processed.")


if __name__ == "__main__":
    main()
