bl_info = {
    "name": "Rig Bridge Toolkit",
    "author": "Custom",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > tab 'Rig Bridge' & tab 'Bone Fix'",
    "description": "Rig Bridge (kendalikan skeleton eksternal pakai Rigify + bake/export FBX) dan Bone Chain Fixer (rapikan tulang kacau), dalam satu addon, panel tetap terpisah",
    "category": "Rigging",
}

import bpy
import mathutils
import os


# ============================================================
# HELPER FUNCTIONS - UMUM (Rig Bridge)
# ============================================================

def ensure_object_mode():
    try:
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass


# ============================================================
# HELPER FUNCTIONS - STEP 2 & 3 (empty + constraint)
# ============================================================

def find_rigify_rig(context, exclude=None):
    """Cari armature di scene yang punya bone berawalan 'DEF-' (hasil generate Rigify)."""
    for obj in context.scene.objects:
        if obj.type == 'ARMATURE' and obj != exclude:
            if any(b.name.startswith("DEF-") for b in obj.pose.bones):
                return obj
    return None


def find_nearest_def_bone(rig_obj, def_bones, world_point):
    """Cari DEF bone yang head-nya (world space) paling dekat dengan world_point."""
    nearest = None
    nearest_dist = None
    for b in def_bones:
        world_head = rig_obj.matrix_world @ b.head
        dist = (world_head - world_point).length
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest = b
    return nearest


def get_or_create_empty(name, size, coll):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'EMPTY':
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = 'PLAIN_AXES'
        coll.objects.link(obj)
    obj.empty_display_size = size
    return obj


def set_copy_transforms(empty_obj, target_armature, subtarget):
    for c in list(empty_obj.constraints):
        if c.type == 'COPY_TRANSFORMS':
            empty_obj.constraints.remove(c)
    c = empty_obj.constraints.new('COPY_TRANSFORMS')
    c.target = target_armature
    c.subtarget = subtarget


def set_bone_copy_constraints(armature_obj, bone_name, target_empty):
    pb = armature_obj.pose.bones[bone_name]
    for ctype in ('COPY_LOCATION', 'COPY_ROTATION'):
        for c in [c for c in pb.constraints if c.type == ctype]:
            pb.constraints.remove(c)
        c = pb.constraints.new(ctype)
        c.target = target_empty


# ============================================================
# HELPER FUNCTIONS - STEP 4 (bake & export FBX)
# ============================================================

def get_bones_hierarchy_order(armature_obj):
    """Urutan pose bone dari root paling atas turun ke ujung hirarki (parent selalu sebelum child)."""
    order = []
    roots = [pb for pb in armature_obj.pose.bones if pb.parent is None]

    def visit(pb):
        order.append(pb)
        for child in pb.children:
            visit(child)

    for r in roots:
        visit(r)
    return order


def bake_bone_manual(context, pb, frame_start, frame_end):
    """
    Bake 1 pose bone secara manual, frame demi frame:
    - frame_set() -> depsgraph update -> pb.matrix sudah hasil constraint (visual)
    - tulis balik pb.matrix ke dirinya sendiri -> ini "membekukan" visual matrix
      itu ke matrix_basis (location/rotation), lepas dari constraint
    - keyframe_insert location + rotation (sesuai rotation_mode aktif)
    Setelah semua frame selesai, constraint bone ini dihapus (setara "Clear Local Constraints").
    Tidak butuh select bone / pindah mode sama sekali, jadi bebas dari masalah Bone.select.
    """
    rot_path = {
        'QUATERNION': "rotation_quaternion",
        'AXIS_ANGLE': "rotation_axis_angle",
    }.get(pb.rotation_mode, "rotation_euler")

    for frame in range(frame_start, frame_end + 1):
        context.scene.frame_set(frame)

        # Ambil visual matrix, decompose, strip scale supaya tidak ada
        # non-uniform scale yang masuk animasi (bikin twist di UE)
        mat = pb.matrix.copy()
        loc, rot, _scale = mat.decompose()
        clean_mat = (
            mathutils.Matrix.Translation(loc)
            @ rot.to_matrix().to_4x4()
        )
        pb.matrix = clean_mat

        pb.keyframe_insert(data_path="location", frame=frame, group=pb.name)
        pb.keyframe_insert(data_path=rot_path,   frame=frame, group=pb.name)

    for c in list(pb.constraints):
        pb.constraints.remove(c)


def get_action_fcurves(action, slot=None):
    """
    Ambil semua FCurve dari sebuah action, kompatibel dengan:
    - Blender lama (<4.4): Action.fcurves langsung
    - Blender 4.4+ : layered/slotted actions (layers -> strips -> channelbag -> fcurves),
      karena Action.fcurves versi lama sudah dihapus total mulai Blender 5.0.
    """
    result = []
    layers = getattr(action, "layers", None)
    if layers:
        slots_to_try = [slot] if slot is not None else list(getattr(action, "slots", []))
        for layer in layers:
            for strip in layer.strips:
                if not hasattr(strip, "channelbag"):
                    continue
                for s in slots_to_try:
                    if s is None:
                        continue
                    try:
                        cb = strip.channelbag(s)
                    except Exception:
                        cb = None
                    if cb:
                        result.extend(cb.fcurves)
        if result:
            return result

    # Fallback untuk Action versi lama (sebelum Blender 5.0 menghapus API legacy ini)
    legacy = getattr(action, "fcurves", None)
    if legacy:
        result.extend(legacy)
    return result


def clean_action_fcurves(action, slot=None, threshold=0.0001):
    """Versi manual dari 'Clean Curves': hapus keyframe yang nilainya nggak beda jauh dari tetangganya."""
    for fcurve in get_action_fcurves(action, slot=slot):
        points = fcurve.keyframe_points
        i = 1
        while i < len(points) - 1:
            prev_v = points[i - 1].co[1]
            cur_v = points[i].co[1]
            next_v = points[i + 1].co[1]
            if abs(cur_v - prev_v) < threshold and abs(cur_v - next_v) < threshold:
                points.remove(points[i])
            else:
                i += 1
        fcurve.update()


def find_bound_meshes(context, armature_obj):
    """Cari semua object MESH yang dibind ke armature_obj, lewat parenting atau Armature modifier."""
    result = []
    for obj in context.scene.objects:
        if obj.type != 'MESH':
            continue
        bound = False
        if obj.parent == armature_obj:
            bound = True
        else:
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object == armature_obj:
                    bound = True
                    break
        if bound:
            result.append(obj)
    return result


def export_fbx_with_preset(filepath, preset_name="mesRig", overrides=None):
    """
    Load Operator Preset FBX export Blender by name (dari dropdown Operator Presets
    di dialog FBX export), exec file preset-nya dengan mock op object supaya semua
    property tertangkap, lalu jalankan export ke filepath.
    overrides: dict property yang di-override dari preset.
    """
    import re

    # Cari file preset di semua lokasi preset Blender
    preset_file = None
    for preset_dir in bpy.utils.preset_paths('operator/export_scene.fbx/'):
        candidate = os.path.join(preset_dir, preset_name + '.py')
        if os.path.exists(candidate):
            preset_file = candidate
            break

    if preset_file is None:
        # Buat daftar preset yang tersedia untuk pesan error yang informatif
        available = []
        for preset_dir in bpy.utils.preset_paths('operator/export_scene.fbx/'):
            if os.path.isdir(preset_dir):
                available += [f[:-3] for f in os.listdir(preset_dir) if f.endswith('.py')]
        raise FileNotFoundError(
            f"Preset '{preset_name}' tidak ditemukan. "
            f"Preset tersedia: {available if available else '(tidak ada)'}"
        )

    with open(preset_file, 'r', encoding='utf-8') as f:
        code = f.read()

    # Hapus baris "op = bpy.context.active_operator" supaya op kita tidak ditimpa
    code = re.sub(
        r'^\s*op\s*=\s*bpy\.context\.active_operator\s*$',
        '',
        code,
        flags=re.MULTILINE,
    )

    # Inject mock op ke namespace exec supaya op.prop = val tertangkap
    mock_op = type('MockOp', (), {})()
    exec(compile(code, preset_file, 'exec'), {'bpy': bpy, 'op': mock_op})

    # Bangun kwargs dari semua property yang di-set di preset
    kwargs = {k: v for k, v in vars(mock_op).items() if not k.startswith('_')}

    # Override yang selalu kita tentukan sendiri
    kwargs['filepath']       = filepath
    kwargs['check_existing'] = False
    if overrides:
        kwargs.update(overrides)

    return bpy.ops.export_scene.fbx(**kwargs)


def open_folder_in_explorer(folder):
    """Buka folder di file explorer sesuai platform."""
    import subprocess, sys
    folder = bpy.path.abspath(folder)
    try:
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])
    except Exception as e:
        print(f"[RigBridge] Gagal buka folder: {e}")


def show_export_done_popup(folder, file_count):
    """Tampilkan popup setelah export selesai dengan tombol buka folder."""
    def draw(self, context):
        layout = self.layout
        layout.label(text=f"{file_count} file FBX berhasil diekspor.", icon='CHECKMARK')
        layout.label(text=folder)
        op = layout.operator("rigbridge.open_export_folder", text="Buka Folder", icon='FILE_FOLDER')
        op.folder = folder
    bpy.context.window_manager.popup_menu(draw, title="Export Selesai", icon='EXPORT')


# ============================================================
# KONSTANTA MAPPING UE SKELETON <-> RIGIFY METARIG
# ============================================================

# Shorthand arah axis lokal armature
_X  = mathutils.Vector((1, 0, 0))
_NX = mathutils.Vector((-1, 0, 0))
_Z  = mathutils.Vector((0, 0, 1))
_NZ = mathutils.Vector((0, 0, -1))

# UE bone name  ->  Rigify metarig bone name
# Bone yang tidak ada mappingnya (twist/special) di-handle lewat tabel di bawah
UE_TO_METARIG_MAP = {
    "pelvis":               "spine",
    "spine_01":             "spine.001",
    "spine_02":             "spine.002",
    "spine_03":             "spine.003",
    "spine_04":             "spine.007",
    "spine_05":             "spine.008",
    "neck_01":              "spine.004",
    "neck_02":              "spine.005",
    "head":                 "spine.006",
    "clavicle_l":           "shoulder.L",
    "upperarm_l":           "upper_arm.L",
    "lowerarm_l":           "forearm.L",
    "hand_l":               "hand.L",
    "middle_metacarpal_l":  "palm.02.L",
    "middle_01_l":          "f_middle.01.L",
    "middle_02_l":          "f_middle.02.L",
    "middle_03_l":          "f_middle.03.L",
    "pinky_metacarpal_l":   "palm.04.L",
    "pinky_01_l":           "f_pinky.01.L",
    "pinky_02_l":           "f_pinky.02.L",
    "pinky_03_l":           "f_pinky.03.L",
    "ring_metacarpal_l":    "palm.03.L",
    "ring_01_l":            "f_ring.01.L",
    "ring_02_l":            "f_ring.02.L",
    "ring_03_l":            "f_ring.03.L",
    "thumb_01_l":           "thumb.01.L",
    "thumb_02_l":           "thumb.02.L",
    "thumb_03_l":           "thumb.03.L",
    "index_metacarpal_l":   "palm.01.L",
    "index_01_l":           "f_index.01.L",
    "index_02_l":           "f_index.02.L",
    "index_03_l":           "f_index.03.L",
    "clavicle_r":           "shoulder.R",
    "upperarm_r":           "upper_arm.R",
    "lowerarm_r":           "forearm.R",
    "hand_r":               "hand.R",
    "middle_metacarpal_r":  "palm.02.R",
    "middle_01_r":          "f_middle.01.R",
    "middle_02_r":          "f_middle.02.R",
    "middle_03_r":          "f_middle.03.R",
    "pinky_metacarpal_r":   "palm.04.R",
    "pinky_01_r":           "f_pinky.01.R",
    "pinky_02_r":           "f_pinky.02.R",
    "pinky_03_r":           "f_pinky.03.R",
    "ring_metacarpal_r":    "palm.03.R",
    "ring_01_r":            "f_ring.01.R",
    "ring_02_r":            "f_ring.02.R",
    "ring_03_r":            "f_ring.03.R",
    "thumb_01_r":           "thumb.01.R",
    "thumb_02_r":           "thumb.02.R",
    "thumb_03_r":           "thumb.03.R",
    "index_metacarpal_r":   "palm.01.R",
    "index_01_r":           "f_index.01.R",
    "index_02_r":           "f_index.02.R",
    "index_03_r":           "f_index.03.R",
    "thigh_r":              "thigh.R",
    "calf_r":               "shin.R",
    "foot_r":               "foot.R",
    "ball_r":               "toe.R",
    "thigh_l":              "thigh.L",
    "calf_l":               "shin.L",
    "foot_l":               "foot.L",
    "ball_l":               "toe.L",
    # IK bones — head snap ke metarig, tapi posisi dihitung ulang setelah pass utama
    "ik_foot_l":            "foot.L",
    "ik_foot_r":            "foot.R",
    "ik_hand_gun":          "hand.R",
    "ik_hand_l":            "hand.L",
    "ik_hand_r":            "hand.R",
}

# Arah tail bone dalam ruang lokal armature UE
# foot_l dan foot_r di-handle khusus (horizontal menuju ball)
UE_TAIL_DIRS = {
    "pelvis":               _NZ,
    "spine_01":             _NZ,
    "spine_02":             _NZ,
    "spine_03":             _NZ,
    "spine_04":             _NZ,
    "spine_05":             _NZ,
    "neck_01":              _NZ,
    "neck_02":              _NZ,
    "head":                 _NZ,
    "clavicle_l":           _NX,
    "upperarm_l":           _NZ,
    "lowerarm_l":           _NZ,
    "hand_l":               _X,
    "middle_metacarpal_l":  _Z,
    "middle_01_l":          _Z,
    "middle_02_l":          _Z,
    "middle_03_l":          _Z,
    "pinky_metacarpal_l":   _Z,
    "pinky_01_l":           _Z,
    "pinky_02_l":           _Z,
    "pinky_03_l":           _Z,
    "ring_metacarpal_l":    _Z,
    "ring_01_l":            _Z,
    "ring_02_l":            _Z,
    "ring_03_l":            _Z,
    "thumb_01_l":           _Z,
    "thumb_02_l":           _Z,
    "thumb_03_l":           _Z,
    "index_metacarpal_l":   _Z,
    "index_01_l":           _Z,
    "index_02_l":           _Z,
    "index_03_l":           _Z,
    "clavicle_r":           _X,
    "upperarm_r":           _Z,
    "lowerarm_r":           _Z,
    "hand_r":               _X,
    "middle_metacarpal_r":  _NZ,
    "middle_01_r":          _NZ,
    "middle_02_r":          _NZ,
    "middle_03_r":          _NZ,
    "pinky_metacarpal_r":   _NZ,
    "pinky_01_r":           _NZ,
    "pinky_02_r":           _NZ,
    "pinky_03_r":           _NZ,
    "ring_metacarpal_r":    _NZ,
    "ring_01_r":            _NZ,
    "ring_02_r":            _NZ,
    "ring_03_r":            _NZ,
    "thumb_01_r":           _NZ,
    "thumb_02_r":           _NZ,
    "thumb_03_r":           _NZ,
    "index_metacarpal_r":   _NZ,
    "index_01_r":           _NZ,
    "index_02_r":           _NZ,
    "index_03_r":           _NZ,
    "thigh_r":              _NZ,
    "calf_r":               _NZ,
    "ball_r":               _NZ,
    "thigh_l":              _Z,
    "calf_l":               _Z,
    "ball_l":               _Z,
    # IK bones pakai arah netral +Y (nggak berpengaruh besar)
    "ik_foot_l":            _NZ,
    "ik_foot_r":            _NZ,
    "ik_hand_gun":          _X,
    "ik_hand_l":            _X,
    "ik_hand_r":            _X,
    "ik_foot_root":         _NZ,
    "ik_hand_root":         _X,
    "interaction":          _X,
    "center_of_mass":       _NZ,
}

# Twist bones: (bone_awal, bone_akhir, t) di mana t=0..1 sepanjang segmen
# t=1/3 berarti dekat bone_awal, t=2/3 dekat bone_akhir
TWIST_ALONG = {
    "upperarm_twist_01_l":  ("upperarm_l",  "lowerarm_l",  1/3),
    "upperarm_twist_02_l":  ("upperarm_l",  "lowerarm_l",  2/3),
    "lowerarm_twist_01_l":  ("lowerarm_l",  "hand_l",      1/3),
    "lowerarm_twist_02_l":  ("lowerarm_l",  "hand_l",      2/3),
    "upperarm_twist_01_r":  ("upperarm_r",  "lowerarm_r",  1/3),
    "upperarm_twist_02_r":  ("upperarm_r",  "lowerarm_r",  2/3),
    "lowerarm_twist_01_r":  ("lowerarm_r",  "hand_r",      1/3),
    "lowerarm_twist_02_r":  ("lowerarm_r",  "hand_r",      2/3),
    "thigh_twist_01_l":     ("thigh_l",     "calf_l",      1/3),
    "thigh_twist_02_l":     ("thigh_l",     "calf_l",      2/3),
    "calf_twist_01_l":      ("calf_l",      "foot_l",      1/3),
    "calf_twist_02_l":      ("calf_l",      "foot_l",      2/3),
    "thigh_twist_01_r":     ("thigh_r",     "calf_r",      1/3),
    "thigh_twist_02_r":     ("thigh_r",     "calf_r",      2/3),
    "calf_twist_01_r":      ("calf_r",      "foot_r",      1/3),
    "calf_twist_02_r":      ("calf_r",      "foot_r",      2/3),
}

# Arah tail twist bone mengikuti parent-nya
TWIST_TAIL_DIR = {
    "upperarm_twist_01_l":  _NZ, "upperarm_twist_02_l":  _NZ,
    "lowerarm_twist_01_l":  _NZ, "lowerarm_twist_02_l":  _NZ,
    "upperarm_twist_01_r":  _Z,  "upperarm_twist_02_r":  _Z,
    "lowerarm_twist_01_r":  _Z,  "lowerarm_twist_02_r":  _Z,
    "thigh_twist_01_l":     _Z,  "thigh_twist_02_l":     _Z,
    "calf_twist_01_l":      _Z,  "calf_twist_02_l":      _Z,
    "thigh_twist_01_r":     _NZ, "thigh_twist_02_r":     _NZ,
    "calf_twist_01_r":      _NZ, "calf_twist_02_r":      _NZ,
}

# ============================================================
# ORIENTASI BONE: label -> remap matrix
#
# Remap matrix R mendefinisikan bagaimana axis lokal UE bone
# memetakan ke axis lokal metarig bone-nya:
#   kolom 0 = arah UE X dalam ruang metarig
#   kolom 1 = arah UE Y dalam ruang metarig
#   kolom 2 = arah UE Z dalam ruang metarig
#
# Label "z": UE Y -> meta Z, UE X -> meta -Y, UE Z -> meta -X
#   col0=(0,-1,0), col1=(0,0,1), col2=(-1,0,0)
# Label lain menyusul setelah konfirmasi.
# ============================================================

LABEL_REMAP = {
    # lambda(meta_X, meta_Y, meta_Z) -> (ue_tail_dir_world, ue_z_axis_world)
    # "z": ue_Y=meta_Z, ue_Z=meta_(-X)   → confirmed user
    "z":  lambda X, Y, Z: ( Z.copy(), -X),
    # "-z": kebalikan z → ue_Y=-meta_Z, ue_Z=meta_X
    "-z": lambda X, Y, Z: (-Z,         X.copy()),
    # "x":  ue_Y=meta_X, ue_Z=meta_(-Y)
    "x":  lambda X, Y, Z: ( X.copy(), -Y),
    # "-x": kebalikan x → ue_Y=-meta_X, ue_Z=meta_Y
    "-x": lambda X, Y, Z: (-X,         Y.copy()),
}

# Label per UE bone sesuai list
BONE_LABEL = {
    "pelvis":               "-z",
    "spine_01":             "-z",
    "spine_02":             "-z",
    "spine_03":             "-z",
    "spine_04":             "-z",
    "spine_05":             "-z",
    "neck_01":              "-z",
    "neck_02":              "-z",
    "head":                 "-z",
    "clavicle_l":           "-x",
    "upperarm_l":           "-z",
    "lowerarm_l":           "-z",
    "hand_l":               "x",
    "middle_metacarpal_l":  "z",
    "middle_01_l":          "z",
    "middle_02_l":          "z",
    "middle_03_l":          "z",
    "pinky_metacarpal_l":   "z",
    "pinky_01_l":           "z",
    "pinky_02_l":           "z",
    "pinky_03_l":           "z",
    "ring_metacarpal_l":    "z",
    "ring_01_l":            "z",
    "ring_02_l":            "z",
    "ring_03_l":            "z",
    "thumb_01_l":           "z",
    "thumb_02_l":           "z",
    "thumb_03_l":           "z",
    "index_metacarpal_l":   "z",
    "index_01_l":           "z",
    "index_02_l":           "z",
    "index_03_l":           "z",
    "clavicle_r":           "-x",
    "upperarm_r":           "z",
    "lowerarm_r":           "z",
    "hand_r":               "x",
    "middle_metacarpal_r":  "-z",
    "middle_01_r":          "-z",
    "middle_02_r":          "-z",
    "middle_03_r":          "-z",
    "pinky_metacarpal_r":   "-z",
    "pinky_01_r":           "-z",
    "pinky_02_r":           "-z",
    "pinky_03_r":           "-z",
    "ring_metacarpal_r":    "-z",
    "ring_01_r":            "-z",
    "ring_02_r":            "-z",
    "ring_03_r":            "-z",
    "thumb_01_r":           "-z",
    "thumb_02_r":           "-z",
    "thumb_03_r":           "-z",
    "index_metacarpal_r":   "-z",
    "index_01_r":           "-z",
    "index_02_r":           "-z",
    "index_03_r":           "-z",
    "thigh_r":              "-z",
    "calf_r":               "-z",
    "foot_r":               "foot_special",
    "ball_r":               "-z",
    "thigh_l":              "z",
    "calf_l":               "z",
    "foot_l":               "foot_special",
    "ball_l":               "z",
}

# Untuk twist bones: metarig bone mana yang jadi patokan orientasi + label-nya
TWIST_META_SOURCE = {
    "upperarm_twist_01_l":  ("upper_arm.L", "-z"),
    "upperarm_twist_02_l":  ("upper_arm.L", "-z"),
    "lowerarm_twist_01_l":  ("forearm.L",   "-z"),
    "lowerarm_twist_02_l":  ("forearm.L",   "-z"),
    "upperarm_twist_01_r":  ("upper_arm.R", "z"),
    "upperarm_twist_02_r":  ("upper_arm.R", "z"),
    "lowerarm_twist_01_r":  ("forearm.R",   "z"),
    "lowerarm_twist_02_r":  ("forearm.R",   "z"),
    "thigh_twist_01_l":     ("thigh.L",     "z"),
    "thigh_twist_02_l":     ("thigh.L",     "z"),
    "calf_twist_01_l":      ("shin.L",      "z"),
    "calf_twist_02_l":      ("shin.L",      "z"),
    "thigh_twist_01_r":     ("thigh.R",     "-z"),
    "thigh_twist_02_r":     ("thigh.R",     "-z"),
    "calf_twist_01_r":      ("shin.R",      "-z"),
    "calf_twist_02_r":      ("shin.R",      "-z"),
}


# ============================================================
# PROPERTIES (Rig Bridge)
# ============================================================

class RB_ActionSelectItem(bpy.types.PropertyGroup):
    """Satu baris action di popup batch export."""
    export: bpy.props.BoolProperty(name="Export", default=True)


class RB_Properties(bpy.types.PropertyGroup):
    source_armature: bpy.props.PointerProperty(
        name="UE Skeleton ",
        description="Armature asli (FBX dari UE/Godot/dll) yang ingin dikendalikan",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    rigify_armature: bpy.props.PointerProperty(
        name="Rig Rigify",
        description="Rig hasil Generate Rigify. Kosongkan untuk auto-detect (cari bone 'DEF-')",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    only_selected_bones: bpy.props.BoolProperty(
        name="Hanya Bone Terpilih",
        description="Kalau aktif, hanya pose bone yang sedang diseleksi di Skeleton Target yang dibuatkan empty",
        default=False,
    )
    empty_size: bpy.props.FloatProperty(name="Ukuran Empty", default=0.05, min=0.001)
    prefix_main: bpy.props.StringProperty(name="Prefix Utama", default="CTRL_")
    prefix_child: bpy.props.StringProperty(name="Prefix Child", default="CLD_")

    export_folder: bpy.props.StringProperty(
        name="Folder Export",
        description="Folder tujuan untuk semua file FBX hasil export",
        subtype='DIR_PATH',
    )
    export_smoothing: bpy.props.EnumProperty(
        name="Smoothing",
        description="Metode smoothing geometry saat export FBX",
        items=[
            ('OFF',          "Normal Only",    "Export normals saja, tanpa smoothing groups"),
            ('FACE',         "Face",           "Export face smoothing"),
            ('EDGE',         "Edge",           "Export edge smoothing"),
            ('SMOOTH_GROUP', "Smooth Groups",  "Export smooth groups"),
        ],
        default='OFF',
    )
    smooth_type: bpy.props.EnumProperty(
        name="Smoothing",
        description="Pengaturan smoothing di bagian Geometry saat export FBX",
        items=[
            ('OFF',          "Normal Only",   "Hanya normals, tanpa smoothing group"),
            ('FACE',         "Face",          "Export face smoothing"),
            ('EDGE',         "Edge",          "Export edge smoothing"),
            ('SMOOTH_GROUP', "Smooth Groups", "Export smooth groups"),
        ],
        default='OFF',
    )
    metarig_for_snap: bpy.props.PointerProperty(
        name="Metarig",
        description="Rigify metarig yang sudah di-fit ke karakter, sebagai patokan posisi snap",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    action_list: bpy.props.CollectionProperty(type=RB_ActionSelectItem)


# ============================================================
# OPERATORS - STEP 1 (helper, pencocokan tetap manual)
# ============================================================

class RB_OT_AddMetarig(bpy.types.Operator):
    bl_idname = "rigbridge.add_metarig"
    bl_label = "Tambah Human Metarig"
    bl_description = "Tambahkan Rigify human metarig (perlu addon Rigify aktif)"

    def execute(self, context):
        if not hasattr(bpy.ops.object, "armature_human_metarig_add"):
            self.report({'ERROR'}, "Addon Rigify belum aktif. Aktifkan di Edit > Preferences > Add-ons.")
            return {'CANCELLED'}
        bpy.ops.object.armature_human_metarig_add()
        self.report(
            {'INFO'},
            "Metarig ditambahkan. Sesuaikan dulu manual: jumlah tulang (jari/spine/dll), "
            "posisi, dan pose sampai sama persis dengan Skeleton Target, baru klik 'Generate Rigify Rig'."
        )
        return {'FINISHED'}


class RB_OT_GenerateRigifyRig(bpy.types.Operator):
    bl_idname = "rigbridge.generate_rigify_rig"
    bl_label = "Generate Rigify Rig"
    bl_description = "Jalankan Rigify generate pada metarig yang sedang aktif (di-select)"

    def execute(self, context):
        if not hasattr(bpy.ops.pose, "rigify_generate"):
            self.report({'ERROR'}, "Addon Rigify belum aktif.")
            return {'CANCELLED'}
        try:
            bpy.ops.pose.rigify_generate()
        except Exception as e:
            self.report({'ERROR'}, f"Gagal generate: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Rig Rigify berhasil di-generate.")
        return {'FINISHED'}


# ============================================================
# OPERATOR - STEP 2 & 3 (full otomatis)
# ============================================================

class RB_OT_BuildControlRig(bpy.types.Operator):
    bl_idname = "rigbridge.build_control_rig"
    bl_label = "Generate Bridge"
    bl_description = "Buat empty (utama + child) dan constraint untuk mengendalikan Skeleton Target dari rig Rigify"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.rb_props
        src = props.source_armature

        if src is None or src.type != 'ARMATURE':
            self.report({'ERROR'}, "Pilih Skeleton Target (armature) dulu.")
            return {'CANCELLED'}

        rig = props.rigify_armature or find_rigify_rig(context, exclude=src)
        if rig is None:
            self.report({'ERROR'}, "Rig Rigify (bone 'DEF-') tidak ditemukan. Generate rig dulu atau pilih manual.")
            return {'CANCELLED'}
        if rig == src:
            self.report({'ERROR'}, "Skeleton Target dan Rig Rigify tidak boleh objek yang sama.")
            return {'CANCELLED'}

        ensure_object_mode()

        def_bones = [b for b in rig.pose.bones if b.name.startswith("DEF-")]
        if not def_bones:
            self.report({'ERROR'}, f"Tidak ada bone berawalan 'DEF-' di '{rig.name}'.")
            return {'CANCELLED'}

        if props.only_selected_bones:
            bones = [pb for pb in src.pose.bones if pb.bone.select]
            if not bones:
                self.report({'ERROR'}, "Tidak ada bone yang terpilih di Skeleton Target.")
                return {'CANCELLED'}
        else:
            bones = list(src.pose.bones)

        coll_name = f"RigBridge_{src.name}"
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            coll = bpy.data.collections.new(coll_name)
            context.scene.collection.children.link(coll)

        SKIP_BONES = {'center_of_mass', 'ik_foot_root', 'ik_hand_root', 'interaction'}

        created = 0
        skipped = []

        for pb in bones:
            if pb.name in SKIP_BONES:
                continue

            bone_world_matrix = src.matrix_world @ pb.matrix
            bone_world_head = src.matrix_world @ pb.head

            nearest_def = find_nearest_def_bone(rig, def_bones, bone_world_head)
            if nearest_def is None:
                skipped.append(pb.name)
                continue

            main_name = f"{props.prefix_main}{pb.name}"
            child_name = f"{props.prefix_child}{pb.name}"

            main_empty = get_or_create_empty(main_name, props.empty_size, coll)
            child_empty = get_or_create_empty(child_name, props.empty_size * 0.6, coll)

            # 1. Taruh empty utama di transform bind DEF bone (sebelum kena constraint)
            def_world_matrix = rig.matrix_world @ nearest_def.matrix
            main_empty.matrix_world = def_world_matrix

            # 2. Parent-kan child ke utama, simpan offset rest pose dengan benar
            if child_empty.parent != main_empty:
                child_empty.parent = main_empty
                child_empty.matrix_parent_inverse = main_empty.matrix_world.inverted()

            # 3. Set transform child = transform bone asli, persis
            child_empty.matrix_world = bone_world_matrix

            # 4. Baru sekarang pasang Copy Transforms di utama -> DEF bone
            set_copy_transforms(main_empty, rig, nearest_def.name)

            # 5. Pasang Copy Location + Copy Rotation di bone asli -> target child
            set_bone_copy_constraints(src, pb.name, child_empty)

            created += 1

        # Constraint root: armature object UE mengikuti bone "root" di Rigify
        # untuk root motion — ini di object level, bukan bone level
        for c in list(src.constraints):
            if c.type in ('COPY_LOCATION', 'COPY_ROTATION') and c.target == rig and c.subtarget == 'root':
                src.constraints.remove(c)

        if 'root' in rig.pose.bones:
            cl = src.constraints.new('COPY_LOCATION')
            cl.name = "RigBridge_Root_Loc"
            cl.target = rig
            cl.subtarget = 'root'

            cr = src.constraints.new('COPY_ROTATION')
            cr.name = "RigBridge_Root_Rot"
            cr.target = rig
            cr.subtarget = 'root'
        else:
            msg_extra = " (bone 'root' tidak ditemukan di Rigify, root motion constraint dilewati)"

        msg = f"Selesai. {created} bone diproses."
        if skipped:
            msg += f" Dilewati: {len(skipped)} bone (tidak ada DEF bone yang cocok)."
        if 'msg_extra' in dir():
            msg += msg_extra

        # Sembunyikan UE skeleton dan collection RigBridge dari viewport
        src.hide_set(True)
        if coll:
            coll.hide_viewport = True

        # Munculkan Rigify control rig apapun kondisinya
        if rig:
            rig.hide_set(False)

        self.report({'INFO'}, msg)
        return {'FINISHED'}


class RB_OT_ClearControlRig(bpy.types.Operator):
    bl_idname = "rigbridge.clear_control_rig"
    bl_label = "Hapus Control Rig"
    bl_description = "Hapus semua empty & constraint yang dibuat addon ini untuk Skeleton Target ini"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.rb_props
        src = props.source_armature
        if src is None:
            self.report({'ERROR'}, "Pilih Skeleton Target dulu.")
            return {'CANCELLED'}

        # Pastikan Object Mode sebelum apapun
        ensure_object_mode()

        for pb in src.pose.bones:
            for ctype in ('COPY_LOCATION', 'COPY_ROTATION'):
                for c in list(pb.constraints):
                    if c.type == ctype and c.target and (
                        c.target.name.startswith(props.prefix_child) or c.target.name.startswith(props.prefix_main)
                    ):
                        pb.constraints.remove(c)

        # Hapus object-level root motion constraint
        for c in list(src.constraints):
            if c.name in ('RigBridge_Root_Loc', 'RigBridge_Root_Rot'):
                src.constraints.remove(c)

        removed = 0
        coll = bpy.data.collections.get(f"RigBridge_{src.name}")
        if coll:
            for obj in list(coll.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
                removed += 1
            bpy.data.collections.remove(coll)

        # Munculkan kembali UE skeleton
        src.hide_set(False)

        # Sembunyikan Rigify control rig
        rig = props.rigify_armature or find_rigify_rig(context, exclude=src)
        if rig:
            rig.hide_set(True)

        self.report({'INFO'}, f"Selesai. {removed} empty dihapus.")
        return {'FINISHED'}


# ============================================================
# OPERATOR - STEP 4 (bake custom + export FBX)
# ============================================================

class RB_OT_BakeAndExportAnim(bpy.types.Operator):
    bl_idname = "rigbridge.export_anim_fbx"
    bl_label = "Export Animation FBX"
    bl_description = (
        "Bake animasi dari rig Rigify ke Skeleton Target bone-per-bone sesuai hirarki, "
        "export FBX animasi, lalu kembalikan constraint asli & hapus action hasil bake"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.rb_props
        src = props.source_armature
        rig = props.rigify_armature or find_rigify_rig(context, exclude=src)

        if src is None or src.type != 'ARMATURE':
            self.report({'ERROR'}, "Pilih Skeleton Target dulu.")
            return {'CANCELLED'}
        if rig is None:
            self.report({'ERROR'}, "Rig Rigify tidak ditemukan. Isi field 'Rig Rigify' atau generate dulu.")
            return {'CANCELLED'}
        if rig.animation_data is None or rig.animation_data.action is None:
            self.report({'ERROR'}, "Rig Rigify belum punya action aktif (animasi) untuk dijadikan acuan nama & frame range.")
            return {'CANCELLED'}

        folder = bpy.path.abspath(props.export_folder)
        if not folder:
            self.report({'ERROR'}, "Isi 'Folder Export' dulu.")
            return {'CANCELLED'}
        os.makedirs(folder, exist_ok=True)

        action_name = rig.animation_data.action.name
        frame_start, frame_end = [int(round(v)) for v in rig.animation_data.action.frame_range]
        original_frame = context.scene.frame_current

        # Simpan dulu target constraint asli (CLD_ empty) sebelum dihapus saat proses bake
        original_targets = {}
        for pb in src.pose.bones:
            for c in pb.constraints:
                if c.type in {'COPY_LOCATION', 'COPY_ROTATION'} and c.target:
                    original_targets[pb.name] = c.target.name

        ensure_object_mode()
        filepath = None

        # Pastikan skeleton visible untuk export (simpan state dulu, restore di finally)
        src_eye_hidden      = src.hide_get()
        src_viewport_hidden = src.hide_viewport
        src.hide_set(False)
        src.hide_viewport = False

        wm = context.window_manager
        context.window.cursor_set('WAIT')

        try:
            context.view_layer.objects.active = src

            # Mulai dari kondisi bersih: lepas action lama (kalau ada sisa dari run sebelumnya)
            if src.animation_data and src.animation_data.action:
                src.animation_data.action = None

            # Bake manual, bone per bone, urut dari root sampai ujung hirarki.
            order = get_bones_hierarchy_order(src)
            n_bones = len(order)
            wm.progress_begin(0, n_bones + 1)
            for i, pb in enumerate(order):
                wm.progress_update(i)
                bake_bone_manual(context, pb, frame_start, frame_end)
            wm.progress_update(n_bones)  # step export

            if not (src.animation_data and src.animation_data.action):
                raise RuntimeError("Bake tidak menghasilkan action di Skeleton Target.")

            baked_action = src.animation_data.action
            baked_action.name = action_name
            clean_action_fcurves(baked_action, slot=getattr(src.animation_data, "action_slot", None))

            for obj in context.scene.objects:
                obj.select_set(False)
            src.select_set(True)
            for mesh_obj in find_bound_meshes(context, src):
                mesh_obj.hide_set(False)
                mesh_obj.hide_viewport = False
                mesh_obj.select_set(True)
            context.view_layer.objects.active = src

            import re
            clean_action_name = re.sub(r'\.\d{3,}$', '', baked_action.name)
            filepath = os.path.join(folder, f"{clean_action_name}.fbx")
            bpy.ops.export_scene.fbx(
                filepath                        = filepath,
                check_existing                  = False,
                use_selection                   = True,
                object_types                    = {'ARMATURE', 'MESH'},
                use_armature_deform_only        = True,
                add_leaf_bones                  = False,
                mesh_smooth_type                = props.smooth_type,
                bake_anim                       = True,
                bake_anim_use_all_bones         = True,
                bake_anim_use_nla_strips        = True,
                bake_anim_use_all_actions       = True,
                bake_anim_force_startend_keying = True,
                bake_anim_simplify_factor       = 0.0,
            )

        finally:
            ensure_object_mode()
            baked_action = src.animation_data.action if src.animation_data else None

            for bone_name, target_name in original_targets.items():
                target_obj = bpy.data.objects.get(target_name)
                if target_obj:
                    set_bone_copy_constraints(src, bone_name, target_obj)

            if src.animation_data:
                src.animation_data.action = None
            if baked_action:
                bpy.data.actions.remove(baked_action, do_unlink=True)

            context.scene.frame_set(original_frame)

            # Kembalikan visibility skeleton ke kondisi semula
            src.hide_set(src_eye_hidden)
            src.hide_viewport = src_viewport_hidden

            wm.progress_end()
            context.window.cursor_set('DEFAULT')

        if filepath:
            show_export_done_popup(folder, 1)
            self.report({'INFO'}, f"Export selesai: {filepath}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Export gagal, lihat console. Constraint sudah dikembalikan ke kondisi semula.")
            return {'CANCELLED'}


class RB_OT_ExportMeshSkeletonFBX(bpy.types.Operator):
    bl_idname = "rigbridge.export_mesh_skeleton_fbx"
    bl_label = "Export Mesh + Skeleton FBX"
    bl_description = (
        "Export skeleton + tiap mesh yang dibind ke skeleton ini (lewat parent/Armature modifier), "
        "masing-masing jadi 1 file FBX terpisah, tanpa animasi"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.rb_props
        src = props.source_armature
        if src is None or src.type != 'ARMATURE':
            self.report({'ERROR'}, "Pilih Skeleton Target dulu.")
            return {'CANCELLED'}

        folder = bpy.path.abspath(props.export_folder)
        if not folder:
            self.report({'ERROR'}, "Isi 'Folder Export' dulu.")
            return {'CANCELLED'}
        os.makedirs(folder, exist_ok=True)

        meshes = find_bound_meshes(context, src)
        if not meshes:
            self.report({'ERROR'}, f"Tidak ada mesh yang terdeteksi terhubung ke '{src.name}' (parent / Armature modifier).")
            return {'CANCELLED'}

        ensure_object_mode()
        exported = []

        # Pastikan skeleton visible untuk export (simpan state dulu, restore setelah selesai)
        src_eye_hidden      = src.hide_get()
        src_viewport_hidden = src.hide_viewport
        src.hide_set(False)
        src.hide_viewport = False

        for mesh_obj in meshes:
            for obj in context.scene.objects:
                obj.select_set(False)
            src.select_set(True)
            mesh_obj.select_set(True)
            context.view_layer.objects.active = src

            import re
            clean_mesh_name = re.sub(r'\.\d{3,}$', '', mesh_obj.name)
            filepath = os.path.join(folder, f"SKM_{clean_mesh_name}.fbx")

            bpy.ops.export_scene.fbx(
                filepath                      = filepath,
                check_existing                = False,
                use_selection                 = True,
                object_types                  = {'ARMATURE', 'MESH'},
                use_armature_deform_only      = True,
                add_leaf_bones                = False,
                mesh_smooth_type              = props.export_smoothing,
                bake_anim                     = False,
                bake_anim_simplify_factor     = 0.0,
            )
            exported.append(filepath)

        # Kembalikan visibility skeleton ke kondisi semula
        src.hide_set(src_eye_hidden)
        src.hide_viewport = src_viewport_hidden

        show_export_done_popup(folder, len(exported))
        self.report({'INFO'}, f"Selesai. {len(exported)} FBX diekspor ke '{folder}'.")
        return {'FINISHED'}


# ============================================================
# OPERATOR - SNAP UE SKELETON KE METARIG
# ============================================================

class RB_OT_SnapUEToMetarig(bpy.types.Operator):
    bl_idname = "rigbridge.snap_ue_to_metarig"
    bl_label = "Snap ke Metarig"
    bl_description = (
        "Pindahkan posisi head bone-bone UE skeleton mengikuti metarig yang sudah di-fit. "
        "Harus dalam Edit Mode di UE skeleton. "
        "Tail diarahkan sesuai preset UE orientation."
    )
    bl_options = {'REGISTER', 'UNDO'}

    # Panjang bone fallback kalau bone aslinya terlalu pendek / nol
    MIN_BONE_LEN = 0.02

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None or obj.type != 'ARMATURE':
            return False
        props = context.scene.rb_props
        return props.metarig_for_snap is not None

    def execute(self, context):
        props   = context.scene.rb_props
        ue_arm  = context.object
        metarig = props.metarig_for_snap

        if metarig == ue_arm:
            self.report({'ERROR'}, "UE skeleton dan metarig tidak boleh objek yang sama.")
            return {'CANCELLED'}

        prev_mode = context.mode
        if prev_mode != 'EDIT_ARMATURE':
            bpy.ops.object.mode_set(mode='EDIT')

        result = self._run(context, ue_arm, metarig)

        if prev_mode != 'EDIT_ARMATURE':
            bpy.ops.object.mode_set(mode='OBJECT')

        return result

    def _run(self, context, ue_arm, metarig):

        # -----------------------------------------------------------------
        # Kumpulkan posisi head semua metarig bone dalam world space
        # (data.bones[n].head_local = local armature space, kita konversi ke world)
        # -----------------------------------------------------------------
        meta_world_head = {}  # meta_bone_name -> mathutils.Vector (world)
        for b in metarig.data.bones:
            meta_world_head[b.name] = metarig.matrix_world @ b.head_local

        ue_inv  = ue_arm.matrix_world.inverted()
        ebs     = ue_arm.data.edit_bones

        # -----------------------------------------------------------------
        # PASS 0: Simpan panjang asli semua bone.
        #         Simpan parent thumb & index SEBELUM apapun diubah
        # -----------------------------------------------------------------
        #         Disconnect use_connect semua bone supaya eb.head bebas digeser.
        # -----------------------------------------------------------------
        original_lengths = {
            eb.name: max(eb.length, self.MIN_BONE_LEN)
            for eb in ebs
        }
        for eb in ebs:
            eb.use_connect = False

        snapped = 0
        missing_meta  = []
        missing_ue    = []

        # -----------------------------------------------------------------
        # PASS 1: Snap head semua bone yang punya mapping langsung
        #         (termasuk ball_l/r supaya pass 2 bisa pakai)
        # -----------------------------------------------------------------
        for ue_name, meta_name in UE_TO_METARIG_MAP.items():
            if ue_name not in ebs:
                missing_ue.append(ue_name)
                continue
            if meta_name not in meta_world_head:
                missing_meta.append(meta_name)
                continue
            eb = ebs[ue_name]
            eb.head = ue_inv @ meta_world_head[meta_name]
            snapped += 1

        # -----------------------------------------------------------------
        # PASS 2: Snap head twist bones (interpolasi antara 2 bone)
        # -----------------------------------------------------------------
        for twist_name, (start_name, end_name, t) in TWIST_ALONG.items():
            if twist_name not in ebs:
                continue
            if start_name not in ebs or end_name not in ebs:
                continue
            h_start = ebs[start_name].head
            h_end   = ebs[end_name].head
            ebs[twist_name].head = h_start.lerp(h_end, t)
            snapped += 1

        # -----------------------------------------------------------------
        # PASS 3: Snap head special / IK bones yang diderive dari posisi lain
        # interaction, ik_foot_root, ik_hand_root, center_of_mass
        # dibiarkan di posisi default — tidak disentuh sama sekali
        # -----------------------------------------------------------------

        # -----------------------------------------------------------------
        # PASS 4: Set orientasi bone sesuai label di list
        #         Orientasi BUKAN dari world axis, tapi dari axis lokal
        #         metarig bone yang menjadi pasangannya masing-masing.
        #
        #         Label "z":
        #           UE bone Y (head→tail) = metarig bone Z axis
        #           UE bone X             = metarig bone -Y axis
        #           UE bone Z             = metarig bone -X axis   (→ align_roll)
        #
        #         Label lain (-z, x, -x) ditambahkan setelah konfirmasi user.
        #         Panjang bone TIDAK diubah — pakai original_lengths.
        # -----------------------------------------------------------------
        meta_world_rot = metarig.matrix_world.to_3x3()
        ue_inv_3x3     = ue_arm.matrix_world.inverted().to_3x3()

        def apply_orient(eb, meta_name, label):
            """Set tail dan roll bone eb sesuai label, relatif terhadap axis metarig bone meta_name."""
            if label not in LABEL_REMAP:
                return  # label belum diimplementasi, skip
            if meta_name not in metarig.data.bones:
                return
            mb     = metarig.data.bones[meta_name]
            # Matrix 3x3 metarig bone dalam world space (kolom = X, Y, Z axis bone)
            mb_mat = meta_world_rot @ mb.matrix_local.to_3x3()
            mX = mb_mat.col[0]
            mY = mb_mat.col[1]
            mZ = mb_mat.col[2]

            # Dapatkan arah UE bone Y (tail) dan Z (roll) dari remap label
            tail_world, z_world = LABEL_REMAP[label](mX, mY, mZ)

            # Konversi ke armature lokal UE
            tail_local = (ue_inv_3x3 @ tail_world).normalized()
            z_local    = (ue_inv_3x3 @ z_world).normalized()

            length = original_lengths.get(eb.name, self.MIN_BONE_LEN)
            eb.tail = eb.head + tail_local * length
            eb.align_roll(z_local)

        # Bone regular — pakai mapping dari BONE_LABEL + UE_TO_METARIG_MAP
        for ue_name, label in BONE_LABEL.items():
            if ue_name not in ebs:
                continue
            meta_name = UE_TO_METARIG_MAP.get(ue_name)
            if meta_name is None:
                continue
            apply_orient(ebs[ue_name], meta_name, label)

        # Twist bones — pakai TWIST_META_SOURCE untuk tahu metarig bone patokannya
        for twist_name, (meta_name, label) in TWIST_META_SOURCE.items():
            if twist_name not in ebs:
                continue
            apply_orient(ebs[twist_name], meta_name, label)

        # ik_hand_r dan ik_hand_gun: ikut orientasi hand_r di UE skeleton
        # (bukan dari metarig — copy tail direction + roll langsung dari hand_r)
        if "hand_r" in ebs:
            hand_r_eb  = ebs["hand_r"]
            hand_r_dir = (hand_r_eb.tail - hand_r_eb.head).normalized()
            hand_r_roll = hand_r_eb.roll
            for ik_name in ("ik_hand_r", "ik_hand_gun"):
                if ik_name not in ebs:
                    continue
                eb     = ebs[ik_name]
                length = original_lengths.get(ik_name, self.MIN_BONE_LEN)
                eb.tail = eb.head + hand_r_dir * length
                eb.roll = hand_r_roll

        # foot_l / foot_r: horizontal menuju ball, orientasi dari metarig foot bone
        for foot_name, ball_name, meta_name in (
            ("foot_l", "ball_l", "foot.L"),
            ("foot_r", "ball_r", "foot.R"),
        ):
            if foot_name not in ebs or ball_name not in ebs:
                continue
            foot_eb = ebs[foot_name]
            ball_eb = ebs[ball_name]

            dir_vec = ball_eb.head - foot_eb.head
            dir_vec.z = 0.0
            if dir_vec.length < 1e-6:
                dir_vec = mathutils.Vector((0, 1, 0))
            else:
                dir_vec.normalize()

            # foot_l: arah tail dibalik (menjauh dari ball, bukan menuju ball)
            if foot_name == "foot_l":
                dir_vec = -dir_vec

            length = original_lengths.get(foot_name, self.MIN_BONE_LEN)
            foot_eb.tail = foot_eb.head + dir_vec * length

            if meta_name in metarig.data.bones:
                mb_mat = meta_world_rot @ metarig.data.bones[meta_name].matrix_local.to_3x3()
                z_local = (ue_inv_3x3 @ (-mb_mat.col[0])).normalized()
                foot_eb.align_roll(z_local)

        # ik_foot_r: ikut orientasi foot_r di UE skeleton (tail direction + roll)
        if "foot_r" in ebs:
            foot_r_eb   = ebs["foot_r"]
            foot_r_dir  = (foot_r_eb.tail - foot_r_eb.head).normalized()
            foot_r_roll = foot_r_eb.roll
            if "ik_foot_r" in ebs:
                eb     = ebs["ik_foot_r"]
                length = original_lengths.get("ik_foot_r", self.MIN_BONE_LEN)
                eb.tail = eb.head + foot_r_dir * length
                eb.roll = foot_r_roll

        # ik_foot_l: ikut orientasi foot_l di UE skeleton (tail direction + roll)
        if "foot_l" in ebs:
            foot_l_eb   = ebs["foot_l"]
            foot_l_dir  = (foot_l_eb.tail - foot_l_eb.head).normalized()
            foot_l_roll = foot_l_eb.roll
            if "ik_foot_l" in ebs:
                eb     = ebs["ik_foot_l"]
                length = original_lengths.get("ik_foot_l", self.MIN_BONE_LEN)
                eb.tail = eb.head + foot_l_dir * length
                eb.roll = foot_l_roll

        # ik_hand_r dan ik_hand_gun: copy orientasi dari hand_r UE skeleton
        # (bukan dari metarig), ukuran tidak diubah
        if "hand_r" in ebs:
            hand_r_mat = ebs["hand_r"].matrix.to_3x3()
            hand_r_Y   = hand_r_mat.col[1]
            hand_r_Z   = hand_r_mat.col[2]
            for ik_name in ("ik_hand_r", "ik_hand_gun"):
                if ik_name not in ebs:
                    continue
                ik_eb  = ebs[ik_name]
                length = original_lengths.get(ik_name, self.MIN_BONE_LEN)
                ik_eb.tail = ik_eb.head + hand_r_Y * length
                ik_eb.align_roll(hand_r_Z)

        # ik_hand_l: ikut orientasi hand_l di UE skeleton
        if "hand_l" in ebs and "ik_hand_l" in ebs:
            hand_l_mat = ebs["hand_l"].matrix.to_3x3()
            hand_l_Y   = hand_l_mat.col[1]
            hand_l_Z   = hand_l_mat.col[2]
            ik_eb  = ebs["ik_hand_l"]
            length = original_lengths.get("ik_hand_l", self.MIN_BONE_LEN)
            ik_eb.tail = ik_eb.head + hand_l_Y * length
            ik_eb.align_roll(hand_l_Z)

        # interaction, ik_foot_root, ik_hand_root, center_of_mass:
        # posisi sudah di-set di Pass 3, orientasi TIDAK disentuh sama sekali

        # -----------------------------------------------------------------
        # -----------------------------------------------------------------
        # Laporan hasil
        # -----------------------------------------------------------------
        msg = f"Selesai. {snapped} bone di-snap."
        if missing_ue:
            msg += f" {len(missing_ue)} bone UE tidak ditemukan di skeleton."
        if missing_meta:
            msg += f" {len(missing_meta)} bone metarig tidak ditemukan."
        if missing_ue or missing_meta:
            print("[RigBridge] Snap UE: bone UE tidak ada di skeleton:", missing_ue)
            print("[RigBridge] Snap UE: bone metarig tidak ada:",        missing_meta)

        self.report({'WARNING'} if (missing_ue or missing_meta) else {'INFO'}, msg)
        return {'FINISHED'}

class RB_OT_BatchExportAnimFBX(bpy.types.Operator):
    bl_idname = "rigbridge.batch_export_anim_fbx"
    bl_label = "Batch Export Animasi FBX"
    bl_description = "Pilih action mana saja yang mau di-export, masing-masing jadi FBX terpisah"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.rb_props
        return props.source_armature is not None and bool(props.export_folder)

    def invoke(self, context, event):
        props = context.scene.rb_props
        # Populate list dari semua action yang ada di file
        props.action_list.clear()
        for action in bpy.data.actions:
            item = props.action_list.add()
            item.name = action.name
            item.export = True
        if not props.action_list:
            self.report({'WARNING'}, "Tidak ada action di file ini.")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        props = context.scene.rb_props
        layout.label(text="Pilih action yang akan di-export:", icon='ACTION')
        layout.separator(factor=0.5)
        for item in props.action_list:
            layout.prop(item, "export", text=item.name)

    def execute(self, context):
        import re
        props  = context.scene.rb_props
        src    = props.source_armature
        rig    = props.rigify_armature or find_rigify_rig(context, exclude=src)
        folder = bpy.path.abspath(props.export_folder)

        if src is None or src.type != 'ARMATURE':
            self.report({'ERROR'}, "Pilih Skeleton Target dulu.")
            return {'CANCELLED'}
        if rig is None:
            self.report({'ERROR'}, "Rig Rigify tidak ditemukan.")
            return {'CANCELLED'}
        if not folder:
            self.report({'ERROR'}, "Isi Folder Export dulu.")
            return {'CANCELLED'}
        os.makedirs(folder, exist_ok=True)

        selected = [item.name for item in props.action_list if item.export]
        if not selected:
            self.report({'WARNING'}, "Tidak ada action yang dipilih.")
            return {'CANCELLED'}

        # Simpan state awal
        original_targets = {}
        for pb in src.pose.bones:
            for c in pb.constraints:
                if c.type in {'COPY_LOCATION', 'COPY_ROTATION'} and c.target:
                    original_targets[pb.name] = c.target.name

        original_frame      = context.scene.frame_current
        original_rig_action = rig.animation_data.action if rig.animation_data else None
        src_eye_hidden      = src.hide_get()
        src_viewport_hidden = src.hide_viewport

        ensure_object_mode()
        src.hide_set(False)
        src.hide_viewport = False

        exported = []
        wm = context.window_manager
        n_bones = len(src.pose.bones)
        total   = len(selected) * (n_bones + 1)
        wm.progress_begin(0, total)
        context.window.cursor_set('WAIT')
        progress = 0

        try:
            for action_name in selected:
                action = bpy.data.actions.get(action_name)
                if action is None:
                    continue

                # Set action ke rig
                if rig.animation_data is None:
                    rig.animation_data_create()
                rig.animation_data.action = action
                frame_start, frame_end = [int(round(v)) for v in action.frame_range]

                # Re-apply constraints (dihapus oleh bake sebelumnya)
                for bone_name, target_name in original_targets.items():
                    target_obj = bpy.data.objects.get(target_name)
                    if target_obj:
                        set_bone_copy_constraints(src, bone_name, target_obj)

                # Bersihkan action lama di src
                if src.animation_data and src.animation_data.action:
                    src.animation_data.action = None

                # Bake
                order = get_bones_hierarchy_order(src)
                for pb in order:
                    wm.progress_update(progress)
                    progress += 1
                    bake_bone_manual(context, pb, frame_start, frame_end)
                wm.progress_update(progress)
                progress += 1  # step export action ini

                if not (src.animation_data and src.animation_data.action):
                    print(f"[RigBridge] Bake gagal untuk action '{action_name}', dilewati.")
                    continue

                baked_action = src.animation_data.action
                clean_action_fcurves(baked_action, slot=getattr(src.animation_data, "action_slot", None))

                # Select objek
                for obj in context.scene.objects:
                    obj.select_set(False)
                src.select_set(True)
                for mesh_obj in find_bound_meshes(context, src):
                    mesh_obj.hide_set(False)
                    mesh_obj.hide_viewport = False
                    mesh_obj.select_set(True)
                context.view_layer.objects.active = src

                clean_name = re.sub(r'\.\d{3,}$', '', action_name)
                filepath = os.path.join(folder, f"{clean_name}.fbx")
                bpy.ops.export_scene.fbx(
                    filepath                        = filepath,
                    check_existing                  = False,
                    use_selection                   = True,
                    object_types                    = {'ARMATURE', 'MESH'},
                    use_armature_deform_only        = True,
                    add_leaf_bones                  = False,
                    mesh_smooth_type                = props.smooth_type,
                    bake_anim                       = True,
                    bake_anim_use_all_bones         = True,
                    bake_anim_use_nla_strips        = True,
                    bake_anim_use_all_actions       = True,
                    bake_anim_force_startend_keying = True,
                    bake_anim_simplify_factor       = 0.0,
                )
                exported.append(filepath)

                # Hapus action hasil bake
                if src.animation_data:
                    src.animation_data.action = None
                bpy.data.actions.remove(baked_action, do_unlink=True)

        finally:
            ensure_object_mode()

            # Restore constraints
            for bone_name, target_name in original_targets.items():
                target_obj = bpy.data.objects.get(target_name)
                if target_obj:
                    set_bone_copy_constraints(src, bone_name, target_obj)

            # Restore rig action
            if rig.animation_data and original_rig_action:
                rig.animation_data.action = original_rig_action

            # Restore hide
            src.hide_set(src_eye_hidden)
            src.hide_viewport = src_viewport_hidden
            context.scene.frame_set(original_frame)

            wm.progress_end()
            context.window.cursor_set('DEFAULT')

        if exported:
            show_export_done_popup(folder, len(exported))
            self.report({'INFO'}, f"Selesai. {len(exported)} FBX diekspor.")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Tidak ada action yang berhasil diekspor.")
            return {'CANCELLED'}


class RB_OT_OpenExportFolder(bpy.types.Operator):
    bl_idname = "rigbridge.open_export_folder"
    bl_label = "Buka Folder"
    bl_description = "Buka folder export di file explorer"

    folder: bpy.props.StringProperty()

    def execute(self, context):
        open_folder_in_explorer(self.folder)
        return {'FINISHED'}


class RB_PT_Panel(bpy.types.Panel):
    bl_label = "Rig Bridge"
    bl_idname = "RB_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rig Bridge"

    def draw(self, context):
        layout = self.layout
        props = context.scene.rb_props

        layout.prop(props, "source_armature")
        layout.separator()

        # --- STEP 1 ---
        box = layout.box()
        box.label(text="tambahkan rigify & metarig", icon='ARMATURE_DATA')
        col = box.column(align=True)
        col.operator("rigbridge.add_metarig", icon='ADD')
        col.operator("rigbridge.generate_rigify_rig", icon='ARMATURE_DATA')

        layout.separator()

        # --- SNAP UE KE METARIG ---
        box_snap = layout.box()
        box_snap.label(text="Snap ke Metarig", icon='BONE_DATA')
        box_snap.prop(props, "metarig_for_snap")
        box_snap.operator("rigbridge.snap_ue_to_metarig", icon='SNAP_ON')

        layout.separator()

        # --- STEP 2 & 3 ---
        box2 = layout.box()
        box2.label(text="Control Rig", icon='POSE_HLT')
        box2.prop(props, "rigify_armature")

        box2.separator()
        box2.operator("rigbridge.build_control_rig", icon='PLAY')
        box2.operator("rigbridge.clear_control_rig", icon='TRASH')

        layout.separator()

        # --- STEP 4 ---
        box4 = layout.box()
        box4.label(text="Export FBX", icon='EXPORT')
        box4.prop(props, "export_folder")
        box4.separator()
        box4.operator("rigbridge.export_anim_fbx", icon='ARMATURE_DATA')
        box4.label(text="(rig Rigify harus sudah punya action aktif)")
        box4.operator("rigbridge.batch_export_anim_fbx", icon='ACTION')
        box4.separator()
        box4.prop(props, "export_smoothing")
        box4.operator("rigbridge.export_mesh_skeleton_fbx", icon='MESH_DATA')


# ============================================================
# BONE CHAIN FIXER (terpisah, tab panel sendiri)
# ============================================================

# Data snapshot disimpan di memori (bukan di file .blend), jadi hilang kalau Blender ditutup.
stored_data = {}             # nama_bone -> {"head": Vector, "tail": Vector}
stored_armature_name = None  # nama armature tempat data ini disimpan, buat cegah salah target


def _armature_edit_poll(context):
    obj = context.object
    return obj is not None and obj.type == 'ARMATURE' and context.mode == 'EDIT_ARMATURE'


def _has_valid_snapshot(context):
    return bool(stored_data) and stored_armature_name == context.object.name


class ARMATURE_OT_save_original(bpy.types.Operator):
    bl_idname = "armature.save_original"
    bl_label = "Save Original"
    bl_description = "Simpan posisi head & tail semua tulang saat ini, supaya bisa dikembalikan nanti"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _armature_edit_poll(context)

    def execute(self, context):
        global stored_data, stored_armature_name

        stored_data.clear()
        for bone in context.object.data.edit_bones:
            stored_data[bone.name] = {
                "head": bone.head.copy(),
                "tail": bone.tail.copy(),
            }
        stored_armature_name = context.object.name

        self.report({'INFO'}, f"Tersimpan {len(stored_data)} tulang dari '{context.object.name}'.")
        return {'FINISHED'}


class ARMATURE_OT_clean_chain(bpy.types.Operator):
    bl_idname = "armature.clean_chain"
    bl_label = "Clean Chain"
    bl_description = (
        "Rapikan chain yang kacau: sambungkan tail tiap parent ke head anaknya, "
        "lalu luruskan tulang ujung (leaf) searah parent-nya. "
        "Butuh Save Original dulu di armature ini"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _armature_edit_poll(context) and _has_valid_snapshot(context)

    def execute(self, context):
        bones = context.object.data.edit_bones

        for bone in bones:
            if bone.parent:
                bone.parent.tail = bone.head

        for bone in bones:
            if len(bone.children) == 0 and bone.parent:
                parent = bone.parent
                direction = (parent.tail - parent.head).normalized()
                length = (bone.tail - bone.head).length
                bone.tail = bone.head + direction * length

        self.report({'INFO'}, "Clean Chain selesai.")
        return {'FINISHED'}


class ARMATURE_OT_restore_original(bpy.types.Operator):
    bl_idname = "armature.restore_original"
    bl_label = "Restore Original"
    bl_description = "Kembalikan posisi head & tail semua tulang ke kondisi saat Save Original ditekan"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _armature_edit_poll(context) and _has_valid_snapshot(context)

    def execute(self, context):
        bones = context.object.data.edit_bones
        restored = 0
        for bone in bones:
            if bone.name in stored_data:
                bone.head = stored_data[bone.name]["head"]
                bone.tail = stored_data[bone.name]["tail"]
                restored += 1

        self.report({'INFO'}, f"{restored} tulang dikembalikan ke posisi semula.")
        return {'FINISHED'}


class ARMATURE_OT_clear_saved(bpy.types.Operator):
    bl_idname = "armature.clear_saved_original"
    bl_label = "Hapus Data Tersimpan"
    bl_description = "Hapus snapshot Save Original dari memori (tidak bisa dibatalkan)"

    @classmethod
    def poll(cls, context):
        return bool(stored_data)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global stored_data, stored_armature_name
        stored_data.clear()
        stored_armature_name = None
        self.report({'INFO'}, "Data tersimpan dihapus.")
        return {'FINISHED'}


class ARMATURE_PT_bone_fix_panel(bpy.types.Panel):
    bl_label = "Bone Chain Fixer"
    bl_idname = "ARMATURE_PT_bone_fix_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rig Bridge"

    def draw(self, context):
        layout = self.layout
        obj = context.object

        status = layout.box()
        if obj is None or obj.type != 'ARMATURE':
            status.label(text="Pilih armature dulu", icon='ERROR')
        elif context.mode != 'EDIT_ARMATURE':
            status.label(text="Masuk Edit Mode dulu", icon='ERROR')
        else:
            status.label(text=f"Armature: {obj.name}", icon='ARMATURE_DATA')

        if stored_data and stored_armature_name:
            status.label(text=f"Tersimpan: {stored_armature_name} ({len(stored_data)} tulang)", icon='CHECKMARK')
        else:
            status.label(text="Belum ada data tersimpan", icon='INFO')

        layout.separator()

        col = layout.column(align=True)
        col.label(text="1. Simpan posisi asli")
        col.operator("armature.save_original", icon='ARMATURE_DATA')

        col.separator()
        col.label(text="2. Rapikan tulang yang kacau")
        col.operator("armature.clean_chain", icon='BONE_DATA')

        col.separator()
        col.label(text="3. Balikin lagi kalau sudah selesai")
        col.operator("armature.restore_original", icon='LOOP_BACK')

        layout.separator()
        layout.operator("armature.clear_saved_original", icon='TRASH')


# ============================================================
# REGISTER
# ============================================================

classes = (
    RB_ActionSelectItem,
    RB_Properties,
    RB_OT_AddMetarig,
    RB_OT_GenerateRigifyRig,
    RB_OT_BuildControlRig,
    RB_OT_ClearControlRig,
    RB_OT_BakeAndExportAnim,
    RB_OT_BatchExportAnimFBX,
    RB_OT_ExportMeshSkeletonFBX,
    RB_OT_OpenExportFolder,
    RB_OT_SnapUEToMetarig,
    RB_PT_Panel,
    ARMATURE_OT_save_original,
    ARMATURE_OT_clean_chain,
    ARMATURE_OT_restore_original,
    ARMATURE_OT_clear_saved,
    ARMATURE_PT_bone_fix_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.rb_props = bpy.props.PointerProperty(type=RB_Properties)


def unregister():
    del bpy.types.Scene.rb_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()