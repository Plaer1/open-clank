"""Closed source and model facts for the configured TRELLIS image recipe."""
RECIPE_ID = 'trellis-image-glb-v1'
TRELLIS_REVISION = '193e0502b5cb3eb5997ec457d5340e95d09b158e'
MODEL_REVISION = '25e0d31ffbebe4b5a97464dd851910efc3002d96'
DINO_REVISION = '7764ea0f912e53c92e82eb78a2a1631e92725fc8'

MODEL_COMPONENTS = {
    'ss_decoder': ('ss_dec_conv3d_16l8_fp16', 'SparseStructureDecoder'),
    'ss_flow': ('ss_flow_img_dit_L_16l8_fp16', 'SparseStructureFlowModel'),
    'slat_gaussian': ('slat_dec_gs_swin8_B_64l8gs32_fp16', 'SLatGaussianDecoder'),
    'slat_radiance': ('slat_dec_rf_swin8_B_64l8r16_fp16', 'SLatRadianceFieldDecoder'),
    'slat_mesh': ('slat_dec_mesh_swin8_B_64l8m256c_fp16', 'SLatMeshDecoder'),
    'slat_flow': ('slat_flow_img_dit_L_64l8p2_fp16', 'SLatFlowModel'),
}
REQUIRED_COMPONENTS = {
    'pipeline':'model/pipeline.json',
    **{role + suffix: 'model/ckpts/' + base + ext for role, (base, _) in MODEL_COMPONENTS.items()
       for suffix, ext in (('_config', '.json'), ('_weights', '.safetensors'))},
    'trellis_base':'trellis/trellis/pipelines/base.py',
    'trellis_image_pipeline':'trellis/trellis/pipelines/trellis_image_to_3d.py',
    'trellis_models_init':'trellis/trellis/models/__init__.py',
    'dino_backbones':'dinov2/dinov2/hub/backbones.py',
    'dino_vision':'dinov2/dinov2/models/vision_transformer.py',
    'dino_weights':'dinov2/weights/dinov2_vitl14_reg4_pretrain.pth',
}
FIXED_SHA256 = {
    'pipeline':'2686b6c9b3b0a100b544d5f9dc7b3ecb15c9d98be9852d68e8731bd8dbe52a42',
    'ss_decoder_config':'646781293f1cda74720de85d1cef50a957fb4aebd9a4bd014e454e32f2330ac5',
    'ss_decoder_weights':'1c76d4a40519aa2d711cc263a8404105231ac26db31d946bed48b84fee79009a',
    'ss_flow_config':'bf69161da7ece9a87394dd4441f0cfa6d4261696a214a8f68388c81fa9a6b5b7',
    'ss_flow_weights':'96dc6bfd4136fd950af564dd16b4ae533c9ba6af8f26c670646b2a9f2789b1db',
    'slat_gaussian_config':'ebe9d3eade6d9cb7662efae109197e97d7ab58aebd5e8ede0c8991dfce88a06a',
    'slat_gaussian_weights':'38c84bcef5ce0af1f48b1b5558dabc7575a13346043c41a7e0610f1fa619a161',
    'slat_radiance_config':'9b46a04602988b9cd83f1d0537de1cb93c4c1b6d35a0bbbcb07bb37bac66b766',
    'slat_radiance_weights':'737da6578d01948016b7c39786113af0d64a46f7922f6b8b5e698b84643be514',
    'slat_mesh_config':'293900795cb69cc972d1a20a4940ee12f7e007952c2ae92d128dda91b96ac317',
    'slat_mesh_weights':'3e87aba94b5786407eb06d0502c1ed0885a0027a3f2b8537bfe15b0a92c01859',
    'slat_flow_config':'c0a9bd57227f55cf75ff02562c27bb05c50174f06955f56a79dea2982fd7a8f9',
    'slat_flow_weights':'693fb2a58ad497bd222007301eeec49d14d60f8c12d2f2f00c221fa747b4c66c',
    'trellis_base':'2b06cdf54571576247bd6601adda7b3772dc23106a4973c35bfc42c4e9253c0c',
    'trellis_image_pipeline':'a9a6eab500dd0693f6f5aa86d42291a4921596988c5c6460aa17fad6fe66b833',
    'trellis_models_init':'afb0321d3b8212be4efdebe16bc80f317135e2c106de3d044896df1d8f46f9ab',
    'dino_backbones':'871fca671b12a9ff02e810654baf509e97ccf461bf8196ce5ddeefff2fd87d3e',
    'dino_vision':'7799a260f2d7d0fe197331d08502fb8c542f9b7424723650f6a39b64fa2639ea',
}
