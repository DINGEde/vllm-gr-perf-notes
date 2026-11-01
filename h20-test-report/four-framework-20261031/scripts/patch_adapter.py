import sys

p = sys.argv[1] if len(sys.argv) > 1 else "benchmarks/competitor/adapters.py"
s = open(p).read()

old = '''        if self.gr and point["beam"] > 256:
            raise Unsupported(
                "this adapter currently validates GR beam widths up to 256; wider-beam integration is not yet verified"
            )'''
new = '''        if self.gr and point["beam"] > 1024:
            raise Unsupported(
                "this adapter validates GR beam widths up to 1024 (vllm-gr MAX_BEAM_WIDTH)"
            )'''
assert old in s, "beam guard not found"
s = s.replace(old, new)

old2 = '''                "max_top_k": width,
            }'''
new2 = '''                "max_top_k": width,
                # vllm-gr needs an explicit per-row candidate count when the beam
                # exceeds MAX_CANDIDATE_WIDTH (256); K stays 256, rows = beam.
                "candidate_width": min(width, 256),
            }'''
assert old2 in s, "constraint table block not found"
s = s.replace(old2, new2)

open(p, "w").write(s)
print("patched", p)
