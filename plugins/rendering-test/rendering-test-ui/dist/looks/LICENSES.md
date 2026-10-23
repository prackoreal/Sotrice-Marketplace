# Texture licenses for looks/pbr

Every texture in `pbr/` is derived from a material downloaded from ambientCG (https://ambientcg.com).

- License: CC0 1.0 Universal (public domain dedication)
  - https://creativecommons.org/publicdomain/zero/1.0/
  - https://docs.ambientcg.com/license/
- ambientCG states (docs.ambientcg.com/license, read 2026-10-23): all assets are CC0 1.0 Universal; you may copy, modify and distribute them, also commercially, without asking permission; credit is optional.
- Attribution line: "Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)"
- Retrieved: 2026-10-23, only from ambientCG (official API and official download links, at most 1 request per second, 1K-JPG packages only).
- Downloaded total: 128.50 MB (cap 140 MB), of which 114.64 MB are the 18 shipped sets and 13.87 MB are 2 sets that were evaluated and rejected (deleted, nothing derived from them is shipped).

## Processing applied to every set

- Derived works are allowed under CC0. Each set is resized to 512x512 (shipped) and 1024x1024 (local, not shipped).
- `albedo.jpg`: Color map decoded sRGB -> linear, luminance Y = 0.2126R + 0.7152G + 0.0722B divided by its mean and scaled so the mean linear value is 0.8 (clamped to [0,1]), re-encoded to sRGB as a 3-channel grey JPEG (q84).
- `nr.jpg`: NormalGL (OpenGL, +Y up) resized and renormalised, packed as R = normal.x, G = normal.y, B = Roughness (JPEG q90, 4:4:4).
- Five sources are 2:1 (1024x512); they were repeated twice along the short axis to make a square tile (no stretching).

## Assets

### stucco: Plaster001 "Plaster 001" (key stucco, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Plaster001
- Download URL used: https://ambientcg.com/get?file=Plaster001_1K-JPG.zip
- Zip: Plaster001_1K-JPG.zip, 7414156 bytes, sha256 `944b4831016e42ace4a89422e4e7190912ca2f7fed6f561354c48ec7bb54d3a4`
- Retrieved: 2026-10-23
- Shipped files: `pbr/stucco/albedo.jpg`, `pbr/stucco/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.800 (0.0% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 3.7 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### stucco_2: Plaster002 "Plaster 002" (key stucco, variant 2, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Plaster002
- Download URL used: https://ambientcg.com/get?file=Plaster002_1K-JPG.zip
- Zip: Plaster002_1K-JPG.zip, 7519098 bytes, sha256 `99d88bd74b8b764eb376f5bb52b5c4a432ff13b989e72882be517c9b2fa13a03`
- Retrieved: 2026-10-23
- Shipped files: `pbr/stucco_2/albedo.jpg`, `pbr/stucco_2/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.800 (0.5% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 3.4 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### brick: Bricks085 "Bricks 085" (key brick, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Bricks085
- Download URL used: https://ambientcg.com/get?file=Bricks085_1K-JPG.zip
- Zip: Bricks085_1K-JPG.zip, 4835502 bytes, sha256 `93202c41a65e6932e77c7c42519ee5433b90150427ae726042c616c188e570a0`
- Retrieved: 2026-10-23
- Shipped files: `pbr/brick/albedo.jpg`, `pbr/brick/nr.jpg`
- Processing: common recipe above; source 1024x512, repeated 1x2 (x by y) to a square tile; albedo gain 1.099 (34.5% of pixels at the clamp, measured mean linear 0.796); nr.jpg mean normal error after JPEG 2.4 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### brick_2: Bricks051 "Bricks 051" (key brick, variant 2, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Bricks051
- Download URL used: https://ambientcg.com/get?file=Bricks051_1K-JPG.zip
- Zip: Bricks051_1K-JPG.zip, 7497159 bytes, sha256 `c23d3d459e1a73f3d8a0b83606571c6172e485f544ff4605d1cb37eb637bfdfd`
- Retrieved: 2026-10-23
- Shipped files: `pbr/brick_2/albedo.jpg`, `pbr/brick_2/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.857 (19.2% of pixels at the clamp, measured mean linear 0.799); nr.jpg mean normal error after JPEG 3.2 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### stone: Bricks066 "Bricks 066" (key stone, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Bricks066
- Download URL used: https://ambientcg.com/get?file=Bricks066_1K-JPG.zip
- Zip: Bricks066_1K-JPG.zip, 7227168 bytes, sha256 `88d35b7a9cd0e2085918e68a31e79a0a71420749bf1bde07a7ede001f6572cdb`
- Retrieved: 2026-10-23
- Shipped files: `pbr/stone/albedo.jpg`, `pbr/stone/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.818 (15.0% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 2.5 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### stone_2: Tiles143 "Tiles 143" (key stone, variant 2, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Tiles143
- Download URL used: https://ambientcg.com/get?file=Tiles143_1K-JPG.zip
- Zip: Tiles143_1K-JPG.zip, 5102319 bytes, sha256 `afbda7adaf4c0048a9224a09ab326fe0f8313e9add1343ad8c09c157a98d881f`
- Retrieved: 2026-10-23
- Shipped files: `pbr/stone_2/albedo.jpg`, `pbr/stone_2/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.802 (3.9% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 0.5 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### concrete: Concrete034 "Concrete 034" (key concrete, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Concrete034
- Download URL used: https://ambientcg.com/get?file=Concrete034_1K-JPG.zip
- Zip: Concrete034_1K-JPG.zip, 3657575 bytes, sha256 `5839d284d94ffb8d2a56df742ec522b13dd311c52dbd42b8fd33f0409ceedb81`
- Retrieved: 2026-10-23
- Shipped files: `pbr/concrete/albedo.jpg`, `pbr/concrete/nr.jpg`
- Processing: common recipe above; source 1024x512, repeated 1x2 (x by y) to a square tile; albedo gain 0.800 (0.0% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 2.3 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### concrete_2: Concrete031 "Concrete 031" (key concrete, variant 2, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Concrete031
- Download URL used: https://ambientcg.com/get?file=Concrete031_1K-JPG.zip
- Zip: Concrete031_1K-JPG.zip, 7823020 bytes, sha256 `3068e551435d3cdae3942017799d8871b940c88ebfaf5c2b87376972ae448fd6`
- Retrieved: 2026-10-23
- Shipped files: `pbr/concrete_2/albedo.jpg`, `pbr/concrete_2/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.808 (10.5% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 2.8 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### wood: WoodSiding009 "Wood Siding 009" (key wood, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=WoodSiding009
- Download URL used: https://ambientcg.com/get?file=WoodSiding009_1K-JPG.zip
- Zip: WoodSiding009_1K-JPG.zip, 4257092 bytes, sha256 `58512fc4df9b7ff0d9adb49bb562e8e7f65b510de6231afadf124505b6274de4`
- Retrieved: 2026-10-23
- Shipped files: `pbr/wood/albedo.jpg`, `pbr/wood/nr.jpg`
- Processing: common recipe above; source 1024x512, repeated 1x2 (x by y) to a square tile; albedo gain 0.801 (2.2% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 1.8 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### metal: CorrugatedSteel005 "Corrugated Steel 005" (key metal, variant 1, role wall)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=CorrugatedSteel005
- Download URL used: https://ambientcg.com/get?file=CorrugatedSteel005_1K-JPG.zip
- Zip: CorrugatedSteel005_1K-JPG.zip, 5894008 bytes, sha256 `4198faced790c75c551750d323ef384ea1ab353463b2067f44f8e5f5ff2d1a09`
- Retrieved: 2026-10-23
- Shipped files: `pbr/metal/albedo.jpg`, `pbr/metal/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.805 (7.9% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 1.3 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### tiles: RoofingTiles014A "Roofing Tiles 014 A" (key tiles, variant 1, role roof)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=RoofingTiles014A
- Download URL used: https://ambientcg.com/get?file=RoofingTiles014A_1K-JPG.zip
- Zip: RoofingTiles014A_1K-JPG.zip, 5001373 bytes, sha256 `b928f75b476df36417e01b6ab60057cb94f65abd035d7a1eaf7ea884ee0f1521`
- Retrieved: 2026-10-23
- Shipped files: `pbr/tiles/albedo.jpg`, `pbr/tiles/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.801 (2.9% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 1.3 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### tiles_2: RoofingTiles012A "Roofing Tiles 012 A" (key tiles, variant 2, role roof)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=RoofingTiles012A
- Download URL used: https://ambientcg.com/get?file=RoofingTiles012A_1K-JPG.zip
- Zip: RoofingTiles012A_1K-JPG.zip, 4757375 bytes, sha256 `ab6ac400cd849c76c636912186bfda8feab2b055fa107c5dbea0d3ebbaadd9cb`
- Retrieved: 2026-10-23
- Shipped files: `pbr/tiles_2/albedo.jpg`, `pbr/tiles_2/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.800 (0.0% of pixels at the clamp, measured mean linear 0.800); nr.jpg mean normal error after JPEG 1.3 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### slate: RoofingTiles003 "Roofing Tiles 003" (key slate, variant 1, role roof)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=RoofingTiles003
- Download URL used: https://ambientcg.com/get?file=RoofingTiles003_1K-JPG.zip
- Zip: RoofingTiles003_1K-JPG.zip, 7675939 bytes, sha256 `0fd7342268bf9e5949331a9a8e679762516605f79a3a3e4cabea1dce68ce15c4`
- Retrieved: 2026-10-23
- Shipped files: `pbr/slate/albedo.jpg`, `pbr/slate/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 1.042 (37.1% of pixels at the clamp, measured mean linear 0.797); nr.jpg mean normal error after JPEG 3.7 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### tar: Gravel024 "Gravel 024" (key tar, variant 1, role roof)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Gravel024
- Download URL used: https://ambientcg.com/get?file=Gravel024_1K-JPG.zip
- Zip: Gravel024_1K-JPG.zip, 9639926 bytes, sha256 `3d1a84efeb0b76b679fbf9f66bf8c36c1980e1d00cf92a3e7bec8ccb0bbb63f5`
- Retrieved: 2026-10-23
- Shipped files: `pbr/tar/albedo.jpg`, `pbr/tar/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.979 (43.8% of pixels at the clamp, measured mean linear 0.795); nr.jpg mean normal error after JPEG 4.4 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### metal_roof: CorrugatedSteel009 "Corrugated Steel 009" (key metal, variant 1, role roof)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=CorrugatedSteel009
- Download URL used: https://ambientcg.com/get?file=CorrugatedSteel009_1K-JPG.zip
- Zip: CorrugatedSteel009_1K-JPG.zip, 3912365 bytes, sha256 `fa917cba8bbf4ceb645cf27e4c1a4a17621d3b1ad44a4125997cdd05662711fb`
- Retrieved: 2026-10-23
- Shipped files: `pbr/metal_roof/albedo.jpg`, `pbr/metal_roof/nr.jpg`
- Processing: common recipe above; source 1024x512, repeated 1x2 (x by y) to a square tile; albedo gain 0.842 (32.2% of pixels at the clamp, measured mean linear 0.799); nr.jpg mean normal error after JPEG 1.2 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### asphalt: Asphalt031 "Asphalt 031" (key asphalt, variant 1, role ground)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=Asphalt031
- Download URL used: https://ambientcg.com/get?file=Asphalt031_1K-JPG.zip
- Zip: Asphalt031_1K-JPG.zip, 8699189 bytes, sha256 `f98114b8fb083fc65925c3b4568b131e37c9a23357d432a16e59676dbc69f964`
- Retrieved: 2026-10-23
- Shipped files: `pbr/asphalt/albedo.jpg`, `pbr/asphalt/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.882 (29.3% of pixels at the clamp, measured mean linear 0.798); nr.jpg mean normal error after JPEG 3.6 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### paving_stones: PavingStones111 "Paving Stones 111" (key paving_stones, variant 1, role ground)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=PavingStones111
- Download URL used: https://ambientcg.com/get?file=PavingStones111_1K-JPG.zip
- Zip: PavingStones111_1K-JPG.zip, 4675374 bytes, sha256 `6229c66cde8538a8278f891cc242275e10176a6fee588a290012368984d9d834`
- Retrieved: 2026-10-23
- Shipped files: `pbr/paving_stones/albedo.jpg`, `pbr/paving_stones/nr.jpg`
- Processing: common recipe above; source 1024x512, repeated 1x2 (x by y) to a square tile; albedo gain 0.832 (20.1% of pixels at the clamp, measured mean linear 0.799); nr.jpg mean normal error after JPEG 1.5 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

### cobblestone: PavingStones070 "Paving Stones 070" (key cobblestone, variant 1, role ground)

- Source: ambientCG, CC0 1.0 Universal (public domain dedication)
- License URLs: https://creativecommons.org/publicdomain/zero/1.0/ and https://docs.ambientcg.com/license/
- Asset page: https://ambientcg.com/view?id=PavingStones070
- Download URL used: https://ambientcg.com/get?file=PavingStones070_1K-JPG.zip
- Zip: PavingStones070_1K-JPG.zip, 9046803 bytes, sha256 `84be05ad935c2a8ea0dd3b82dd18c9e1faf7eeae222b85d96beb14ed16276132`
- Retrieved: 2026-10-23
- Shipped files: `pbr/cobblestone/albedo.jpg`, `pbr/cobblestone/nr.jpg`
- Processing: common recipe above; source 1024x1024; albedo gain 0.892 (38.7% of pixels at the clamp, measured mean linear 0.797); nr.jpg mean normal error after JPEG 3.6 deg
- Attribution: Contains textures from ambientCG.com, CC0 1.0 (no attribution required, credited anyway)

## Evaluated and rejected downloads (not shipped)

- Plaster003: Plaster003_1K-JPG.zip, 7879772 bytes, sha256 `3a47249643273c82c53d8d31c9caef43aec940a4c926cf0c74858a8e96631bf1`. rejected after inspection: busy swirled patches and scratches (unique features, not 'no strong pattern'); replaced by Plaster002.
- Concrete046: Concrete046_1K-JPG.zip, 5989511 bytes, sha256 `72bf4321acbb39ddbc3b786f5996813b2a3cede12efefb47955e97ea9668985b`. rejected after inspection: large rectangular repair-like patch in the lower middle (unique feature, visible repetition); replaced by Concrete031.
