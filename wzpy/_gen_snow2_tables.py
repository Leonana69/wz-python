"""Extract the SNOW 2.0 constant tables from MapleLib's C# source and emit a
Python module. Parsing the source directly avoids hand-transcription errors
across the ~1500 hex constants.
"""
import re
import sys

SRC = r"D:/Git/MapleLib/MapleLib/WzLib/MSFile/Snow2CryptoTransform.cs"
OUT = r"D:/Git/wz-python/wzpy/_snow2_tables.py"

text = open(SRC, "r", encoding="utf-8-sig").read()

# Each table: `... uint[] NAME = { ...hex... };`
TABLE_RE = re.compile(
    r"uint\[\]\s+(snow_\w+)\s*=\s*\{(.*?)\};",
    re.DOTALL,
)
HEX_RE = re.compile(r"0x[0-9A-Fa-f]+")

tables = {}
for m in TABLE_RE.finditer(text):
    name = m.group(1)
    body = m.group(2)
    nums = [int(h, 16) for h in HEX_RE.findall(body)]
    tables[name] = nums

expected = {
    "snow_alpha_mul": 256,
    "snow_alphainv_mul": 256,
    "snow_T0": 256,
    "snow_T1": 256,
    "snow_T2": 256,
    "snow_T3": 256,
}
errs = []
for name, count in expected.items():
    if name not in tables:
        errs.append(f"missing table {name}")
    elif len(tables[name]) != count:
        errs.append(f"{name}: got {len(tables[name])} entries, expected {count}")
    elif any(v > 0xFFFFFFFF or v < 0 for v in tables[name]):
        errs.append(f"{name}: value out of uint32 range")
if errs:
    print("\n".join(errs), file=sys.stderr)
    sys.exit(1)

# Cross-check the documented derivation T1/T2/T3 = byte-rotations of T0.
def rotl_word(w, bytes_):
    w &= 0xFFFFFFFF
    bits = bytes_ * 8
    return ((w << bits) | (w >> (32 - bits))) & 0xFFFFFFFF

for i in range(256):
    assert rotl_word(tables["snow_T0"][i], 1) == tables["snow_T1"][i], i
    assert rotl_word(tables["snow_T0"][i], 2) == tables["snow_T2"][i], i
    assert rotl_word(tables["snow_T0"][i], 3) == tables["snow_T3"][i], i
print("T1/T2/T3 verified as byte rotations of T0 (extraction self-consistent).")


def fmt(name, nums):
    lines = [f"{name} = ("]
    for i in range(0, len(nums), 8):
        chunk = ", ".join(f"0x{v:08X}" for v in nums[i:i + 8])
        lines.append(f"    {chunk},")
    lines.append(")")
    return "\n".join(lines)


header = (
    '"""SNOW 2.0 constant tables.\n\n'
    "AUTO-GENERATED from MapleLib/WzLib/MSFile/Snow2CryptoTransform.cs by\n"
    "wzpy/_gen_snow2_tables.py — do not edit by hand. The four T-tables are\n"
    "byte-rotations of one another (T_k = rotl(T0, k bytes)); the alpha tables\n"
    "are the GF(2^8) multiply-by-alpha / alpha-inverse lookups.\n"
    '"""\n\n'
)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(header)
    for name in ("snow_alpha_mul", "snow_alphainv_mul",
                 "snow_T0", "snow_T1", "snow_T2", "snow_T3"):
        f.write(fmt(name, tables[name]))
        f.write("\n\n")
print(f"wrote {OUT}")
