# Rig Bridge Toolkit

Rig Bridge Toolkit is a Blender addon designed to bridge external skeletons (such as Unreal Engine FBX) with Rigify, enabling a clean animation workflow and reliable FBX export.

## Features

- Bridge external skeletons to Rigify using empties and constraints  
- Automatic control rig generation  
- Manual per-bone animation baking (stable for Unreal Engine)  
- Export animation FBX (game-ready)  
- Export mesh + skeleton separately  
- Snap UE skeleton to fitted metarig  
- Bone Chain Fixer for cleaning broken bone hierarchies  

## Requirements

- Blender 3.6 or newer :contentReference[oaicite:0]{index=0}  
- Rigify addon enabled  

## Installation

1. Open Blender  
2. Go to **Edit > Preferences > Add-ons**  
3. Click **Install**  
4. Select `RigBridge.py`  
5. Enable the addon  

The addon will appear in:  
- View3D Sidebar (Press **N**)  
- Tabs: **Rig Bridge** and **Bone Fix** :contentReference[oaicite:1]{index=1}  

## Usage

### Basic Workflow

1. Import your external skeleton (FBX from Unreal Engine or others)  
2. Add a Rigify Human Metarig  
3. Adjust the metarig to match your character  
4. Generate the Rigify rig  
5. Click **Generate Bridge**  
6. Animate using Rigify controls  
7. Export animation as FBX  

### Export Animation

- Bake animation from Rigify to target skeleton  
- Export FBX with clean transforms  
- Automatically restores constraints after export  

### Export Mesh + Skeleton

- Exports each bound mesh as a separate FBX  
- Includes armature without animation  

## Notes

- Designed for game pipelines (especially Unreal Engine)  
- Uses constraint-based bridging for accurate motion transfer  
- Baking removes dependency on Rigify for final export  

## License

Free to use
