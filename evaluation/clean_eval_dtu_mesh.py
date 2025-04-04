import numpy as np
import cv2 as cv
from glob import glob
import trimesh
import torch
import open3d as o3d
import sklearn.neighbors as skln
from tqdm import tqdm
from scipy.io import loadmat
import multiprocessing as mp
import argparse, os, sys
import cv2
from skimage.morphology import binary_dilation, disk
import torch.nn.functional as F


from pathlib import Path

sys.path.append("../")

image_width = 1554
image_height = 1162

def load_K_Rt_from_P(filename, P=None):
    if P is None:
        lines = open(filename).read().splitlines()
        if len(lines) == 4:
            lines = lines[1:]
        lines = [[x[0], x[1], x[2], x[3]] for x in (x.split(" ") for x in lines)]
        P = np.asarray(lines).astype(np.float32).squeeze()

    out = cv.decomposeProjectionMatrix(P)
    K = out[0]
    R = out[1]
    t = out[2]

    K = K / K[2, 2]
    intrinsics = np.eye(4)
    intrinsics[:3, :3] = K

    pose = np.eye(4, dtype=np.float32)
    pose[:3, :3] = R.transpose()  # ? why need transpose here
    pose[:3, 3] = (t[:3] / t[3])[:, 0]

    return intrinsics, pose  # ! return cam2world matrix here


def clean_points_by_mask(points, scan, imgs_idx=None, minimal_vis=0, mask_dilated_size=11):
    cameras = np.load('{}/scan{}/cameras.npz'.format(DTU_DIR, scan))
    mask_lis = sorted(glob('{}/scan{}/mask/*.png'.format(DTU_DIR, scan)))
    n_images = 49 if scan < 83 else 64
    inside_mask = np.zeros(len(points))

    if imgs_idx is None:
        imgs_idx = [i for i in range(n_images)]

    for i in imgs_idx:
        P = cameras['world_mat_{}'.format(i)]
        pts_image = np.matmul(P[None, :3, :3], points[:, :, None]).squeeze() + P[None, :3, 3]
        pts_image = pts_image / pts_image[:, 2:]
        pts_image = np.round(pts_image).astype(np.int32) + 1

        mask_image = cv.imread(mask_lis[i])
        kernel_size = mask_dilated_size  # default 101
        kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (kernel_size, kernel_size))
        mask_image = cv.dilate(mask_image, kernel, iterations=1)
        mask_image = (mask_image[:, :, 0] > 128)

        mask_image = np.concatenate([np.ones([1, image_width]), mask_image, np.ones([1, image_width])], axis=0)
        mask_image = np.concatenate([np.ones([image_height, 1]), mask_image, np.ones([image_width, 1])], axis=1)

        in_mask = (pts_image[:, 0] >= 0) * (pts_image[:, 0] <= image_width) * (pts_image[:, 1] >= 0) * (
                pts_image[:, 1] <= image_height) > 0
        curr_mask = mask_image[(pts_image[:, 1].clip(0, image_height+1), pts_image[:, 0].clip(0, image_width+1))]

        curr_mask = curr_mask.astype(np.float32) * in_mask

        inside_mask += curr_mask

    return inside_mask > minimal_vis


def clean_points_by_visualhull(points, scan, imgs_idx=None, minimal_vis=0, mask_dilated_size=11):
    cameras = np.load('{}/scan{}/scan/cameras.npz'.format(DTU_DIR, scan))
    mask_lis = sorted(glob('{}/scan{}/scan/mask/*.png'.format(DTU_DIR, scan)))
    n_images = 49 if scan < 83 else 64
    outside_mask = np.zeros(len(points))

    if imgs_idx is None:
        imgs_idx = [i for i in range(n_images)]

    for i in imgs_idx:
        P = cameras['world_mat_{}'.format(i)]
        pts_image = np.matmul(P[None, :3, :3], points[:, :, None]).squeeze() + P[None, :3, 3]
        pts_image = pts_image / pts_image[:, 2:]
        pts_image = np.round(pts_image).astype(np.int32) + 1

        mask_image = cv.imread(mask_lis[i])
        kernel_size = mask_dilated_size  # default 101
        kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (kernel_size, kernel_size))
        mask_image = cv.dilate(mask_image, kernel, iterations=1)
        mask_image = (mask_image[:, :, 0] < 128)  # * outside the mask

        mask_image = np.concatenate([np.ones([1, 1600]), mask_image, np.ones([1, 1600])], axis=0)
        mask_image = np.concatenate([np.ones([1202, 1]), mask_image, np.ones([1202, 1])], axis=1)

        border = 50
        in_mask = (pts_image[:, 0] >= (0 + border)) * (pts_image[:, 0] <= (1600 - border)) * (
                pts_image[:, 1] >= (0 + border)) * (
                          pts_image[:, 1] <= (1200 - border)) > 0
        curr_mask = mask_image[(pts_image[:, 1].clip(0, 1201), pts_image[:, 0].clip(0, 1601))]

        curr_mask = curr_mask.astype(np.float32) * in_mask

        outside_mask += curr_mask

    return outside_mask < 5


def clean_mesh_faces_by_mask(mesh_file, new_mesh_file, scan, imgs_idx, minimal_vis=0, mask_dilated_size=11):
    old_mesh = trimesh.load(mesh_file)
    old_vertices = old_mesh.vertices[:]
    old_faces = old_mesh.faces[:]
    mask = clean_points_by_mask(old_vertices, scan, imgs_idx, minimal_vis, mask_dilated_size)
    indexes = np.ones(len(old_vertices)) * -1
    indexes = indexes.astype(np.int64)
    indexes[np.where(mask)] = np.arange(len(np.where(mask)[0]))

    faces_mask = mask[old_faces[:, 0]] & mask[old_faces[:, 1]] & mask[old_faces[:, 2]]
    new_faces = old_faces[np.where(faces_mask)]
    new_faces[:, 0] = indexes[new_faces[:, 0]]
    new_faces[:, 1] = indexes[new_faces[:, 1]]
    new_faces[:, 2] = indexes[new_faces[:, 2]]
    new_vertices = old_vertices[np.where(mask)]

    new_mesh = trimesh.Trimesh(new_vertices, new_faces)

    # ! if colmap trim=7, comment these
    # meshes = new_mesh.split(only_watertight=False)
    # new_mesh = meshes[np.argmax([len(mesh.faces) for mesh in meshes])]

    new_mesh.export(new_mesh_file)


def clean_mesh_faces_by_visualhull(mesh_file, new_mesh_file, scan, imgs_idx, minimal_vis=0, mask_dilated_size=11):
    old_mesh = trimesh.load(mesh_file)
    old_vertices = old_mesh.vertices[:]
    old_faces = old_mesh.faces[:]
    mask = clean_points_by_visualhull(old_vertices, scan, imgs_idx, minimal_vis, mask_dilated_size)
    indexes = np.ones(len(old_vertices)) * -1
    indexes = indexes.astype(np.int64)
    indexes[np.where(mask)] = np.arange(len(np.where(mask)[0]))

    faces_mask = mask[old_faces[:, 0]] & mask[old_faces[:, 1]] & mask[old_faces[:, 2]]
    new_faces = old_faces[np.where(faces_mask)]
    new_faces[:, 0] = indexes[new_faces[:, 0]]
    new_faces[:, 1] = indexes[new_faces[:, 1]]
    new_faces[:, 2] = indexes[new_faces[:, 2]]
    new_vertices = old_vertices[np.where(mask)]

    new_mesh = trimesh.Trimesh(new_vertices, new_faces)

    # ! if colmap trim=7, comment these
    # meshes = new_mesh.split(only_watertight=False)
    # new_mesh = meshes[np.argmax([len(mesh.faces) for mesh in meshes])]

    new_mesh.export(new_mesh_file)


def clean_mesh_by_faces_num(mesh, faces_num=500):
    old_vertices = mesh.vertices[:]
    old_faces = mesh.faces[:]

    cc = trimesh.graph.connected_components(mesh.face_adjacency, min_len=faces_num)
    mask = np.zeros(len(mesh.faces), dtype=np.bool)
    mask[np.concatenate(cc)] = True

    indexes = np.ones(len(old_vertices)) * -1
    indexes = indexes.astype(np.int64)
    indexes[np.where(mask)] = np.arange(len(np.where(mask)[0]))

    faces_mask = mask[old_faces[:, 0]] & mask[old_faces[:, 1]] & mask[old_faces[:, 2]]
    new_faces = old_faces[np.where(faces_mask)]
    new_faces[:, 0] = indexes[new_faces[:, 0]]
    new_faces[:, 1] = indexes[new_faces[:, 1]]
    new_faces[:, 2] = indexes[new_faces[:, 2]]
    new_vertices = old_vertices[np.where(mask)]

    new_mesh = trimesh.Trimesh(new_vertices, new_faces)

    return new_mesh


def clean_outliers(old_mesh_file, new_mesh_file, faces_num=500, keep_largest=True):
    new_mesh = trimesh.load(old_mesh_file)

    if keep_largest:
        meshes = new_mesh.split(only_watertight=False)
        new_mesh = meshes[np.argmax([len(mesh.faces) for mesh in meshes])]
    else:
        new_mesh = clean_mesh_by_faces_num(new_mesh, faces_num)

    new_mesh.export(new_mesh_file)

def get_path_components(path):
    path = Path(path)
    ppath = str(path.parent)
    stem = str(path.stem)
    ext = str(path.suffix)
    return ppath, stem, ext


def sample_single_tri(input_):
    n1, n2, v1, v2, tri_vert = input_
    c = np.mgrid[:n1 + 1, :n2 + 1]
    c += 0.5
    c[0] /= max(n1, 1e-7)
    c[1] /= max(n2, 1e-7)
    c = np.transpose(c, (1, 2, 0))
    k = c[c.sum(axis=-1) < 1]  # m2
    q = v1 * k[:, :1] + v2 * k[:, 1:] + tri_vert
    return q


def write_vis_pcd(file, points, colors):
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    pcd.colors = o3d.utility.Vector3dVector(colors)
    o3d.io.write_point_cloud(file, pcd)


def cull_scan_2dgs(scan, mesh_path, result_mesh_file, instance_dir):
    def load_K_Rt_from_P(filename, P=None):
        if P is None:
            lines = open(filename).read().splitlines()
            if len(lines) == 4:
                lines = lines[1:]
            lines = [[x[0], x[1], x[2], x[3]] for x in (x.split(" ") for x in lines)]
            P = np.asarray(lines).astype(np.float32).squeeze()
        out = cv2.decomposeProjectionMatrix(P)
        K = out[0]
        R = out[1]
        t = out[2]
        K = K / K[2, 2]
        intrinsics = np.eye(4)
        intrinsics[:3, :3] = K
        pose = np.eye(4, dtype=np.float32)
        pose[:3, :3] = R.transpose()
        pose[:3, 3] = (t[:3] / t[3])[:, 0]
        return intrinsics, pose

    # load poses
    image_dir = '{0}/scan{1}/images'.format(instance_dir, scan)
    image_paths = sorted(glob(os.path.join(image_dir, "*.png")))
    n_images = len(image_paths)
    cam_file = '{0}/scan{1}/cameras.npz'.format(instance_dir, scan)
    camera_dict = np.load(cam_file)
    scale_mats = [camera_dict['scale_mat_%d' % idx].astype(np.float32) for idx in range(n_images)]
    world_mats = [camera_dict['world_mat_%d' % idx].astype(np.float32) for idx in range(n_images)]

    intrinsics_all = []
    pose_all = []
    for scale_mat, world_mat in zip(scale_mats, world_mats):
        P = world_mat @ scale_mat
        P = P[:3, :4]
        intrinsics, pose = load_K_Rt_from_P(None, P)
        intrinsics_all.append(torch.from_numpy(intrinsics).float())
        pose_all.append(torch.from_numpy(pose).float())

    # load mask
    mask_dir = '{0}/scan{1}/mask'.format(instance_dir, scan)
    mask_paths = sorted(glob(os.path.join(mask_dir, "*.png")))
    masks = []
    for p in mask_paths:
        mask = cv2.imread(p)
        masks.append(mask)

    # hard-coded image shape
    W, H = 1600, 1200

    # load mesh
    mesh = trimesh.load(mesh_path)

    # load transformation matrix

    vertices = mesh.vertices

    # project and filter
    vertices = torch.from_numpy(vertices).cuda()
    vertices = torch.cat((vertices, torch.ones_like(vertices[:, :1])), dim=-1)
    vertices = vertices.permute(1, 0)
    vertices = vertices.float()

    sampled_masks = []
    for i in tqdm(range(n_images), desc="Culling mesh given masks"):
        pose = pose_all[i]
        w2c = torch.inverse(pose).cuda()
        intrinsic = intrinsics_all[i].cuda()

        with torch.no_grad():
            # transform and project
            cam_points = intrinsic @ w2c @ vertices
            pix_coords = cam_points[:2, :] / (cam_points[2, :].unsqueeze(0) + 1e-6)
            pix_coords = pix_coords.permute(1, 0)
            pix_coords[..., 0] /= W - 1
            pix_coords[..., 1] /= H - 1
            pix_coords = (pix_coords - 0.5) * 2
            valid = ((pix_coords > -1.) & (pix_coords < 1.)).all(dim=-1).float()

            # dialate mask similar to unisurf
            maski = masks[i][:, :, 0].astype(np.float32) / 256.
            maski = torch.from_numpy(binary_dilation(maski, disk(24))).float()[None, None].cuda()

            sampled_mask = \
            F.grid_sample(maski, pix_coords[None, None], mode='nearest', padding_mode='zeros', align_corners=True)[
                0, -1, 0]

            sampled_mask = sampled_mask + (1. - valid)
            sampled_masks.append(sampled_mask)

    sampled_masks = torch.stack(sampled_masks, -1)
    # filter

    mask = (sampled_masks > 0.).all(dim=-1).cpu().numpy()
    face_mask = mask[mesh.faces].all(axis=1)

    mesh.update_vertices(mask)
    mesh.update_faces(face_mask)

    # transform vertices to world
    scale_mat = scale_mats[0]
    mesh.vertices = mesh.vertices * scale_mat[0, 0] + scale_mat[:3, 3][None]
    mesh.export(result_mesh_file)
    del mesh


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # TODO: change these 5 arguments -------------
    parser.add_argument('--datadir', type=str, default='../data/DTU')
    parser.add_argument('--expdir', type=str, default='../output/dtu/scan24/meshes')
    parser.add_argument('--mesh_name', type=str, default='meshsdf_merge_15000.ply')
    parser.add_argument('--scan', type=int, default=24)
    parser.add_argument('--dataset_dir', type=str, default='../data/DTU_GTpoints/')
    # -----------------
    parser.add_argument('--gt', type=str, help='ground truth')
    parser.add_argument('--mode', type=str, default='mesh', choices=['mesh', 'pcd'])
    parser.add_argument('--vis_out_dir', type=str, default='.')
    parser.add_argument('--downsample_density', type=float, default=0.2)
    parser.add_argument('--patch_size', type=float, default=60)
    parser.add_argument('--max_dist', type=float, default=20)
    parser.add_argument('--visualize_threshold', type=float, default=10)
    parser.add_argument('--log', type=str, default=None)
    args = parser.parse_args()

    ### clean
    # scans = [24, 37, 40, 55, 63, 65, 69, 83, 97, 105, 106, 110, 114, 118, 122]
    scans = [args.scan]
    mask_kernel_size = 11
    DTU_DIR = args.datadir

    for scan in scans:
        # TODO: change here
        if not args.expdir.endswith('meshes'):
            args.expdir = os.path.join(args.expdir, 'meshes')
        base_path = args.expdir
        new_path = os.path.join(base_path, 'meshes_clean')
        mesh_name = args.mesh_name
        print("processing scan%d" % scan)
        if not os.path.exists(new_path):
            os.makedirs(new_path)
        old_mesh_file = os.path.join(base_path, mesh_name)
        clean_mesh_file = os.path.join(new_path, 'clean_'+mesh_name)
        cull_scan_2dgs(scan, old_mesh_file, clean_mesh_file, DTU_DIR)

        print("finish processing scan%d" % scan)
    ### eval
    mp.freeze_support()

    GT_POINTS_DIR = os.path.join(args.dataset_dir, "Points/stl")

    for scan in scans:
        print("processing scan%d" % scan)
        base_dir = new_path

        args.data = os.path.join(base_dir, "clean_"+mesh_name)

        if not os.path.exists(args.data):
            continue

        args.gt = os.path.join(GT_POINTS_DIR, "stl%03d_total.ply" % scan)
        args.vis_out_dir = base_dir
        args.scan = scan
        os.makedirs(args.vis_out_dir, exist_ok=True)

        dist_thred1 = 1
        dist_thred2 = 2

        thresh = args.downsample_density

        if args.mode == 'mesh':
            pbar = tqdm(total=9)
            pbar.set_description('read data mesh')
            data_mesh = o3d.io.read_triangle_mesh(args.data)

            vertices = np.asarray(data_mesh.vertices)
            triangles = np.asarray(data_mesh.triangles)
            tri_vert = vertices[triangles]

            pbar.update(1)
            pbar.set_description('sample pcd from mesh')
            v1 = tri_vert[:, 1] - tri_vert[:, 0]
            v2 = tri_vert[:, 2] - tri_vert[:, 0]
            l1 = np.linalg.norm(v1, axis=-1, keepdims=True)
            l2 = np.linalg.norm(v2, axis=-1, keepdims=True)
            area2 = np.linalg.norm(np.cross(v1, v2), axis=-1, keepdims=True)
            non_zero_area = (area2 > 0)[:, 0]
            l1, l2, area2, v1, v2, tri_vert = [
                arr[non_zero_area] for arr in [l1, l2, area2, v1, v2, tri_vert]
            ]
            thr = thresh * np.sqrt(l1 * l2 / area2)
            n1 = np.floor(l1 / thr)
            n2 = np.floor(l2 / thr)

            with mp.Pool() as mp_pool:
                new_pts = mp_pool.map(sample_single_tri,
                                      ((n1[i, 0], n2[i, 0], v1[i:i + 1], v2[i:i + 1], tri_vert[i:i + 1, 0]) for i in
                                       range(len(n1))), chunksize=1024)

            new_pts = np.concatenate(new_pts, axis=0)
            data_pcd = np.concatenate([vertices, new_pts], axis=0)

        elif args.mode == 'pcd':
            pbar = tqdm(total=8)
            pbar.set_description('read data pcd')
            data_pcd_o3d = o3d.io.read_point_cloud(args.data)
            data_pcd = np.asarray(data_pcd_o3d.points)

        pbar.update(1)
        pbar.set_description('random shuffle pcd index')
        shuffle_rng = np.random.default_rng()
        shuffle_rng.shuffle(data_pcd, axis=0)

        pbar.update(1)
        pbar.set_description('downsample pcd')
        nn_engine = skln.NearestNeighbors(n_neighbors=1, radius=thresh, algorithm='kd_tree', n_jobs=-1)
        nn_engine.fit(data_pcd)
        rnn_idxs = nn_engine.radius_neighbors(data_pcd, radius=thresh, return_distance=False)
        mask = np.ones(data_pcd.shape[0], dtype=np.bool_)
        for curr, idxs in enumerate(rnn_idxs):
            if mask[curr]:
                mask[idxs] = 0
                mask[curr] = 1
        data_down = data_pcd[mask]

        pbar.update(1)
        pbar.set_description('masking data pcd')
        obs_mask_file = loadmat(f'{args.dataset_dir}/ObsMask/ObsMask{args.scan}_10.mat')
        ObsMask, BB, Res = [obs_mask_file[attr] for attr in ['ObsMask', 'BB', 'Res']]
        BB = BB.astype(np.float32)

        patch = args.patch_size
        inbound = ((data_down >= BB[:1] - patch) & (data_down < BB[1:] + patch * 2)).sum(axis=-1) == 3
        data_in = data_down[inbound]

        data_grid = np.around((data_in - BB[:1]) / Res).astype(np.int32)
        grid_inbound = ((data_grid >= 0) & (data_grid < np.expand_dims(ObsMask.shape, 0))).sum(axis=-1) == 3
        data_grid_in = data_grid[grid_inbound]
        in_obs = ObsMask[data_grid_in[:, 0], data_grid_in[:, 1], data_grid_in[:, 2]].astype(np.bool_)
        data_in_obs = data_in[grid_inbound][in_obs]

        pbar.update(1)
        pbar.set_description('read STL pcd')
        stl_pcd = o3d.io.read_point_cloud(args.gt)
        stl = np.asarray(stl_pcd.points)

        pbar.update(1)
        pbar.set_description('compute data2stl')
        nn_engine.fit(stl)
        dist_d2s, idx_d2s = nn_engine.kneighbors(data_in_obs, n_neighbors=1, return_distance=True)
        max_dist = args.max_dist
        mean_d2s = dist_d2s[dist_d2s < max_dist].mean()

        precision_1 = len(dist_d2s[dist_d2s < dist_thred1]) / len(dist_d2s)
        precision_2 = len(dist_d2s[dist_d2s < dist_thred2]) / len(dist_d2s)

        pbar.update(1)
        pbar.set_description('compute stl2data')
        ground_plane = loadmat(f'{args.dataset_dir}/ObsMask/Plane{args.scan}.mat')['P']

        stl_hom = np.concatenate([stl, np.ones_like(stl[:, :1])], -1)
        above = (ground_plane.reshape((1, 4)) * stl_hom).sum(-1) > 0

        stl_above = stl[above]

        nn_engine.fit(data_in)
        dist_s2d, idx_s2d = nn_engine.kneighbors(stl_above, n_neighbors=1, return_distance=True)
        mean_s2d = dist_s2d[dist_s2d < max_dist].mean()

        recall_1 = len(dist_s2d[dist_s2d < dist_thred1]) / len(dist_s2d)
        recall_2 = len(dist_s2d[dist_s2d < dist_thred2]) / len(dist_s2d)

        pbar.update(1)
        pbar.set_description('visualize error')
        vis_dist = args.visualize_threshold
        R = np.array([[1, 0, 0]], dtype=np.float64)
        G = np.array([[0, 1, 0]], dtype=np.float64)
        B = np.array([[0, 0, 1]], dtype=np.float64)
        W = np.array([[1, 1, 1]], dtype=np.float64)
        data_color = np.tile(B, (data_down.shape[0], 1))
        data_alpha = dist_d2s.clip(max=vis_dist) / vis_dist
        data_color[np.where(inbound)[0][grid_inbound][in_obs]] = R * data_alpha + W * (1 - data_alpha)
        data_color[np.where(inbound)[0][grid_inbound][in_obs][dist_d2s[:, 0] >= max_dist]] = G
        write_vis_pcd(f'{args.vis_out_dir}/vis_{args.scan:03}_d2gt.ply', data_down, data_color)
        stl_color = np.tile(B, (stl.shape[0], 1))
        stl_alpha = dist_s2d.clip(max=vis_dist) / vis_dist
        stl_color[np.where(above)[0]] = R * stl_alpha + W * (1 - stl_alpha)
        stl_color[np.where(above)[0][dist_s2d[:, 0] >= max_dist]] = G
        write_vis_pcd(f'{args.vis_out_dir}/vis_{args.scan:03}_gt2d.ply', stl, stl_color)

        pbar.update(1)
        pbar.set_description('done')
        pbar.close()
        over_all = (mean_d2s + mean_s2d) / 2

        fscore_1 = 2 * precision_1 * recall_1 / (precision_1 + recall_1 + 1e-6)
        fscore_2 = 2 * precision_2 * recall_2 / (precision_2 + recall_2 + 1e-6)

        print(f'over_all: {over_all}; mean_d2gt: {mean_d2s}; mean_gt2d: {mean_s2d}.')
        print(f'precision_1mm: {precision_1};  recall_1mm: {recall_1};  fscore_1mm: {fscore_1}')
        print(f'precision_2mm: {precision_2};  recall_2mm: {recall_2};  fscore_2mm: {fscore_2}')

        pparent, stem, ext = get_path_components(args.data)
        if args.log is None:
            path_log = os.path.join(pparent, 'eval_result.txt')
        else:
            path_log = args.log
        with open(path_log, 'w+') as fLog:
            fLog.write(f'over_all {np.round(over_all, 3)} '
                       f'mean_d2gt {np.round(mean_d2s, 3)} '
                       f'mean_gt2d {np.round(mean_s2d, 3)} \n'
                       f'precision_1mm {np.round(precision_1, 3)} '
                       f'recall_1mm {np.round(recall_1, 3)} '
                       f'fscore_1mm {np.round(fscore_1, 3)} \n'
                       f'precision_2mm {np.round(precision_2, 3)} '
                       f'recall_2mm {np.round(recall_2, 3)} '
                       f'fscore_2mm {np.round(fscore_2, 3)} \n'
                       f'[{stem}] \n')
