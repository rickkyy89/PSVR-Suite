"""Rotation-guided matching of partial front/side/rear PSVR LED patterns."""

import json
from pathlib import Path
import numpy as np
from itertools import combinations

from .led_pose import FrontLedTracker, LedPose

# Model x right/y down when seen frontally -> headset wearer x right/y up.
MODEL_TO_IMU = np.diag([-1.,-1.,1.])


def rotation_from_quaternion(q):
    q = np.asarray(q,dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q)-1) > .01:
        raise ValueError("invalid IMU quaternion")
    w,x,y,z = q/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                     [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                     [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])


def load_rotation_rig(path):
    rig = json.loads(Path(path).read_text(encoding="utf-8"))
    if rig.get("schema") != "psvr.camera.rotation_led_rig" or rig.get("version") != 1:
        raise ValueError("unsupported rotation LED rig")
    points = np.asarray(rig["object_points_m"],float)
    normals = np.asarray(rig["outward_normals"],float)
    labels = rig.get("labels",[])
    if (points.shape != (9,3) or normals.shape != (9,3) or len(set(labels)) != 9
            or not np.isfinite(points).all() or not np.isfinite(normals).all()
            or np.any(np.linalg.norm(normals,axis=1) < .5)):
        raise ValueError("nine finite LED points, distinct labels and nonzero normals required")
    return rig,points,normals/np.linalg.norm(normals,axis=1)[:,None]


class ImuLedTracker:
    def __init__(self,intrinsics,points,normals,labels,max_rms_px=2.5):
        self.front = FrontLedTracker(intrinsics,points[:5],max_rms_px)
        self.cv2 = self.front.cv2
        self.k,self.dist = self.front.k,self.front.dist
        self.points,self.normals,self.labels = points,normals,labels
        self.reset()

    def reset(self):
        self.front.reset()
        self.alignment = None
        self.translation = None
        self.imu = None
        self.epoch = None
        self.session_id = None
        self.last_match = None
        self.velocity = np.zeros((3,1))

    def set_imu(self,sample):
        if sample is not None and sample.get("session_id") is not None:
            session = sample["session_id"]
            epoch = sample.get("recenter_epoch", 0)
            if session != self.session_id:
                self.reset()
                self.session_id = session
            elif self.epoch is not None and epoch != self.epoch:
                # Absolute q_sensor and A do not change on driver recenter.
                self.velocity = np.zeros((3,1))
                self.front.reset()
            self.epoch = epoch
        self.imu = sample

    def rotation(self,now):
        if self.imu is None or not self.imu.get("valid"):
            return None
        age = now-self.imu["monotonic_s"]
        if not 0 <= age <= .15:
            return None
        try:
            q = self.imu.get("quaternion_sensor_wxyz")
            if q is None:
                q = self.imu["quaternion_wxyz"]
            imu_rotation = rotation_from_quaternion(q)
        except (ValueError,KeyError):
            return None
        if self.session_id is None and self.epoch is not None and self.imu.get("recenter_count",0) != self.epoch:
            self.alignment = None
            self.translation = None
            self.epoch = None
            self.last_match = None
            self.front.reset()
        return imu_rotation

    def predicted(self,rotation,translation,relaxed=False):
        camera_points = (rotation@self.points.T).T+translation.reshape(1,3)
        normals = (rotation@self.normals.T).T
        toward_camera = -camera_points/np.linalg.norm(camera_points,axis=1)[:,None]
        # Normals are approximate: allow grazing candidates during matching,
        # rather than enforcing exact visibility at the LED transition angle.
        visible = np.where((np.sum(normals*toward_camera,axis=1) > (-.15 if relaxed else .02))&(camera_points[:,2] > .1))[0]
        rvec = self.cv2.Rodrigues(rotation)[0]
        projected = self.cv2.projectPoints(self.points,rvec,translation,self.k,self.dist)[0].reshape(-1,2)
        return visible,projected

    def matching_rotation(self, imu_rotation):
        """Optical model rotation; alignment remains the immutable world basis."""
        return self.alignment@imu_rotation@MODEL_TO_IMU

    def search_region(self,width,height,now):
        imu_rotation = self.rotation(now)
        if self.alignment is None or imu_rotation is None or self.translation is None:
            return None,1.0
        if self.last_match is None or now-self.last_match > .5:
            return None,1.0
        rotation = self.matching_rotation(imu_rotation)
        forecast = self.translation+self.velocity*min(.1,max(0.,now-self.last_match))
        visible,projected = self.predicted(rotation,forecast,relaxed=True)
        if len(visible) < 2:
            return None,1.0
        low,high = projected[visible].min(axis=0),projected[visible].max(axis=0)
        span = float(np.max(high-low))
        margin = max(20.,span*.3)
        low = np.clip(np.floor(low-margin),[0,0],[width,height]).astype(int)
        high = np.clip(np.ceil(high+margin),[0,0],[width,height]).astype(int)
        if np.any(high-low < 5):
            return None,1.0
        return (*low.tolist(),*high.tolist()), min(1.0,span/max(self.front.acquisition_span or 100,1))

    def solve_translation(self,rotation,ids,image):
        if len(ids) < 2 or np.max(np.linalg.norm(self.points[ids,None,:]-self.points[None,ids,:],axis=2)) < .04:
            return None
        normalized=self.cv2.undistortPoints(image.reshape(-1,1,2),self.k,self.dist).reshape(-1,2)
        rotated=(rotation@self.points[ids].T).T
        matrix=[]
        values=[]
        for (x,y),(px,py,pz) in zip(normalized,rotated):
            matrix.extend([[1,0,-x],[0,1,-y]])
            values.extend([x*pz-px,y*pz-py])
        matrix=np.array(matrix)
        if np.linalg.cond(matrix) > 500:
            return None
        translation=np.linalg.lstsq(matrix,values,rcond=None)[0].reshape(3,1)
        if np.min(rotated[:,2]+translation[2,0]) <= .1 or not .15 < translation[2,0] < 4:
            return None
        return translation

    def match_pattern(self,rotation,centers,visible,forecast,now):
        rvec=self.cv2.Rodrigues(rotation)[0]
        _,projected=self.predicted(rotation,forecast,relaxed=True)
        width=float(np.max(np.ptp(projected[visible],axis=0))) if len(visible) else 0
        gate=max(20.,min(80.,width*.4))
        seed_edges=[]
        for i in visible:
            distances=np.linalg.norm(centers-projected[i],axis=1)
            for j in np.argsort(distances)[:2]:
                if distances[j] <= gate:
                    seed_edges.append((int(i),int(j)))
        span=float(np.max(np.ptp(centers,axis=0)))
        limit=max(3.,self.front.reprojection_limit(span))
        # 75Hz observations cannot legitimately teleport 25cm. Allow centroid /
        # depth noise plus up to 2m/s measured motion, expanding on short loss.
        # Reject bad correspondence hypotheses before they update the motion
        # prior or the adaptive output filter (which otherwise follows spikes).
        elapsed=max(0.,now-self.last_match)
        maximum_jump=min(.25,.015+2*elapsed)
        solutions={}
        for first,second in combinations(seed_edges,2):
            if first[0] == second[0] or first[1] == second[1]:
                continue
            ids=np.array([first[0],second[0]])
            image=centers[[first[1],second[1]]]
            translation=self.solve_translation(rotation,ids,image)
            if translation is None or np.linalg.norm(translation-self.translation) > maximum_jump:
                continue
            # Seed hypotheses are scored against the entire constellation, not
            # accepted just because two arbitrary rays happen to fit.
            for _ in range(2):
                projected=self.cv2.projectPoints(self.points,rvec,translation,self.k,self.dist)[0].reshape(-1,2)
                edges=sorted((float(np.linalg.norm(projected[i]-center)),int(i),j)
                    for i in visible for j,center in enumerate(centers))
                assignment=[]
                used_ids=set()
                used_candidates=set()
                for distance,i,j in edges:
                    if distance > limit*2:
                        break
                    if i not in used_ids and j not in used_candidates:
                        assignment.append((i,j))
                        used_ids.add(i)
                        used_candidates.add(j)
                if len(assignment) < 2:
                    break
                ids=np.array([a[0] for a in assignment])
                image=centers[[a[1] for a in assignment]]
                fitted=self.solve_translation(rotation,ids,image)
                if fitted is None:
                    break
                translation=fitted
            else:
                if np.linalg.norm(translation-self.translation) > maximum_jump:
                    continue
                reprojection=self.cv2.projectPoints(self.points[ids],rvec,translation,self.k,self.dist)[0].reshape(-1,2)
                rms=float(np.sqrt(np.mean(np.sum((reprojection-image)**2,axis=1))))
                if rms > limit:
                    continue
                if len(ids) < self.minimum_matched_leds():
                    continue
                key=tuple(sorted(assignment))
                prior=float(np.linalg.norm(translation-forecast))
                # Do not saturate the continuity penalty at 3cm: that made
                # widely different depths effectively equally plausible.
                cost=rms+prior/.01*.25
                candidate=(len(ids),cost,rms,translation,ids,image,limit)
                if key not in solutions or cost < solutions[key][1]:
                    solutions[key]=candidate
        ordered=sorted(solutions.values(),key=lambda item:(-item[0],item[1]))
        if not ordered:
            return None,"Pattern non associato: nessuna costellazione coerente con IMU"
        best=ordered[0]
        if len(ordered) > 1:
            other=ordered[1]
            if (other[0] == best[0] and other[1]-best[1] < .2
                    and np.linalg.norm(other[3]-best[3]) > .03):
                return None,"Pattern ambiguo: due associazioni con profondita diverse"
        return best,None

    def minimum_matched_leds(self):
        return 2

    def _recover_absolute(self,candidates,now,imu_rotation):
        # Forget the optical motion prior once, not the activation's world basis.
        # Keep frontend confirmations across subsequent recovery frames.
        if self.translation is not None or self.last_match is not None:
            self.translation = None
            self.last_match = None
            self.velocity = np.zeros((3,1))
            self.front.reset()
        initial = self.front.update(candidates,now)
        if not initial.valid:
            return initial
        rotation = self.matching_rotation(imu_rotation)
        ids = np.arange(5)
        image = initial.image_points
        translation = self.solve_translation(rotation,ids,image)
        if translation is None:
            return LedPose(False,"Reaggancio frontale: traslazione incompatibile con IMU")
        rvec = self.cv2.Rodrigues(rotation)[0]
        projected = self.cv2.projectPoints(self.points[ids],rvec,translation,self.k,self.dist)[0].reshape(-1,2)
        rms = float(np.sqrt(np.mean(np.sum((projected-image)**2,axis=1))))
        span = float(np.max(np.ptp(image,axis=0)))
        limit = self.front.reprojection_limit(span)
        if not np.isfinite(rms) or rms > limit:
            return LedPose(False,"Reaggancio frontale: corrispondenze incompatibili con IMU",
                           rms_px=rms,rms_limit_px=limit,image_span_px=span)
        self.translation = translation
        self.last_match = now
        pose = LedPose(True,"imu_guided_front_reacquired",image,rvec,translation,rms,limit,span)
        pose.led_labels = self.labels[:5]
        pose.imu_age_s = now-self.imu["monotonic_s"]
        return pose

    def update(self,candidates,now):
        imu_rotation = self.rotation(now)
        if imu_rotation is None:
            state = self.imu.get("state","attesa") if self.imu else "attesa"
            if self.session_id is not None:
                return LedPose(False,"IMU driver: %s; attesa export UDP fresco (nessuna calibrazione HID locale)" % state)
            if state == "attesa":
                return LedPose(False,"Premi AVVIA IMU quando puoi tenere il visore fermo")
            if state == "calibrating":
                calibration = self.imu.get("calibration",{})
                remaining = max(0.,(calibration.get("target_ms",8000)-calibration.get("elapsed_ms",0))/1000)
                return LedPose(False,"IMU: RESTA FERMO - calibrazione %.1f secondi rimanenti" % remaining)
            return LedPose(False,"IMU %s: tieni il visore fermo per calibrare; nessuna posa fresca" % state)
        if self.alignment is None:
            initial = self.front.update(candidates,now)
            if not initial.valid:
                return initial
            rotation = self.cv2.Rodrigues(initial.rvec)[0]
            self.alignment = rotation@MODEL_TO_IMU.T@imu_rotation.T
            self.translation = initial.tvec.copy()
            self.velocity = np.zeros((3,1))
            self.epoch = self.imu.get("recenter_count",0)
            self.last_match = now
            initial.led_labels = self.labels[:5]
            initial.imu_age_s = now-self.imu["monotonic_s"]
            return initial
        if self.last_match is None or now-self.last_match > 2:
            if self.session_id is not None:
                return self._recover_absolute(candidates,now,imu_rotation)
            self.alignment = None
            self.translation = None
            self.front.reset()
            return LedPose(False,"Pattern perso: torna frontale per riallineare camera e IMU")
        rotation = self.matching_rotation(imu_rotation)
        forecast=self.translation+self.velocity*min(.1,max(0.,now-self.last_match))
        visible,projected = self.predicted(rotation,forecast,relaxed=True)
        centers = np.asarray([c["center_px"] for c in candidates],float).reshape(-1,2)
        if len(centers) < 2 or not np.isfinite(centers).all():
            return LedPose(False,"Pattern parziale: servono almeno due luci separate")
        matched,reason=self.match_pattern(rotation,centers,visible,forecast,now)
        if matched is None:
            return LedPose(False,reason)
        _,_,rms,translation,ids,image,limit=matched
        rvec = self.cv2.Rodrigues(rotation)[0]
        span = float(np.max(np.ptp(image,axis=0)))
        dt=now-self.last_match
        if dt > .001:
            measured=(translation-self.translation)/dt
            speed=np.linalg.norm(measured)
            if speed > 2:
                measured*=2/speed
            self.velocity=self.velocity*.75+measured*.25
        self.translation = translation
        self.last_match = now
        pose = LedPose(True,"imu_guided_pattern",image,rvec,translation,rms,limit,span)
        pose.led_labels = [self.labels[i] for i in ids]
        pose.predicted_labels = [self.labels[i] for i in visible]
        pose.imu_age_s = now-self.imu["monotonic_s"]
        return pose
