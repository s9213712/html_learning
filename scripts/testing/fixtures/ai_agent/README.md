# AI Agent image probe fixture

`qwen_squat_double_v_white_longhair_cat_ears.png` is a stable input fixture for
the three manual AI Agent image-edit probes in `scripts/testing/`.

Probe reports and generated images must go through
`scripts.test_artifacts.test_artifact_path`, which defaults outside the source
checkout. Never place probe results in this fixture directory.
