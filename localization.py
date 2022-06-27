import copy
from abc import ABC, abstractmethod
import filterpy.common
import numpy as np

from robots.robots import ConstantAccelerationRobot2D, RandomAccelerationRobot2D, ControlledRobot2D, TwoRobotSystem, \
    RotatingRobot2D
from utils import Util
from scipy import signal
from utils.animator import Animator
import filterpy.kalman


class BaseLocalization(ABC):
    def __init__(self, robot_system: TwoRobotSystem, count=50):
        self.robot_system = robot_system
        self.count = count
        self.dt = robot_system.dt
        self.idx_loc = 0
        robot_system.get_r_measurement()

        # Every measurement we get two possible locations for the robot. The initial position is not available
        # through measurements, since the algorithm makes use of change in distance.
        self.measured_positions = np.zeros((count, 2, 2))
        self.chosen_measurements = np.zeros((count, 2))
        self.chosen_measurements[0] = [None, None]
        self.measured_positions[0] = [[None, None], [None, None]]

        self.filtered_dr = []
        self.filtered_r = [self.robot_system.measured_r[0]]

        self.estimated_positions = np.zeros((count, 2))

    def calculate_possible_positions(self):
        prev_rs = self.robot_system.measured_r
        if self.robot_system.is_noisy:
            if len(prev_rs) < 3:
                dr = signal.savgol_filter(deriv=1, x=prev_rs,
                                          window_length=len(prev_rs), polyorder=1, delta=self.dt)[-1]
                r = signal.savgol_filter(deriv=0, x=prev_rs,
                                         window_length=min(len(prev_rs), 50), polyorder=1, delta=self.dt)[-1]
            else:
                dr = signal.savgol_filter(deriv=1, x=prev_rs,
                                                   window_length=min(len(prev_rs), 20), polyorder=1, delta=self.dt)[-1]
                r = signal.savgol_filter(deriv=0, x=prev_rs,
                                         window_length=min(len(prev_rs), 20), polyorder=1, delta=self.dt)[-1]
        else:
            dr = (prev_rs[-1] - prev_rs[-2]) / self.dt
            r = prev_rs[-1]

        self.filtered_r.append(r)
        self.filtered_dr.append(dr)

        v = self.robot_system.measured_v[-1]
        s = np.linalg.norm(v)
        alpha = np.arctan2(v[1], v[0])
        theta = np.arccos(Util.clamp(dr / s, -1, 1))

        pos1 = [r * np.cos(alpha + theta), r * np.sin(alpha + theta)] + self.robot_system.anchor_robot.pos
        pos2 = [r * np.cos(alpha - theta), r * np.sin(alpha - theta)] + self.robot_system.anchor_robot.pos
        return [pos1, pos2]

    def animate_results(self, title, save=False, plot_error_figures=False):
        target_positions = self.robot_system.all_target_positions.T
        anchor_positions = self.robot_system.all_anchor_positions.T
        estimated_positions = self.estimated_positions.T

        measurements_t = self.measured_positions[:].T
        measurements_reshaped = np.stack((measurements_t[0], measurements_t[1]), axis=1)
        measurements_1 = measurements_reshaped[0]
        measurements_2 = measurements_reshaped[1]

        real_r = np.array(self.robot_system.real_r)
        measured_r = np.array(self.robot_system.measured_r)

        ani = Animator(title, self.count, self.idx_loc, anchor_positions, target_positions, estimated_positions,
                       measurements_1, measurements_2, real_r, measured_r, save, plot_error_figures,
                       np.array(self.filtered_dr) * self.dt, np.linalg.norm(self.robot_system.measured_v, axis=1))
        ani.run()

    @abstractmethod
    def run(self):
        pass


class MotionBasedLocalization(BaseLocalization):
    def __init__(self, robot_system: TwoRobotSystem, count=50, kf=None, known_initial_pos=False):
        super().__init__(robot_system, count)
        self.kf = kf
        self.localized = False
        self.prev_v = robot_system.get_v_measurement()

        if known_initial_pos:
            self.localized = True
            self.idx_loc = 0
            self.estimated_positions[0] = robot_system.target_robot.pos - robot_system.anchor_robot.pos

    def run(self):
        def find_max_similarity(prev_positions, new_positions):
            max_sim = -2
            max_position = None
            for pos in new_positions:
                for prev in prev_positions:
                    sim = Util.cos_similarity(pos, prev)
                    if sim > max_sim:
                        max_sim = sim
                        max_position = pos
            return max_position

        for i in range(1, self.count):
            self.robot_system.update()
            self.robot_system.get_r_measurement()
            measured_v = self.robot_system.get_v_measurement()
            measurement1, measurement2 = self.calculate_possible_positions()
            self.measured_positions[i] = [measurement1, measurement2]

            if self.localized:
                # Compare measurements to moving average of previous positions
                window_length = 5
                window = list(range(max(self.idx_loc, i-window_length), i))
                prev_pos = np.mean(self.estimated_positions[window], axis=0)
                closest_measurement = Util.closest_to(prev_pos, [measurement1, measurement2])
                self.chosen_measurements[i] = closest_measurement

                if self.kf:
                    self.kf.predict()
                    self.kf.update(np.concatenate((closest_measurement, measured_v)))
                    closest_measurement = [self.kf.x[0], self.kf.x[1]]
                self.estimated_positions[i] = closest_measurement
            else:
                similarity = Util.cos_similarity(self.prev_v, measured_v)
                if similarity < .97 and i > 20:
                    self.localized = True
                    prev1 = self.measured_positions[i-1][0]
                    prev2 = self.measured_positions[i-1][1]

                    max_pos = find_max_similarity([prev1, prev2], [measurement1, measurement2])
                    self.estimated_positions[i] = max_pos

                    if self.kf:
                        self.kf.x = np.concatenate((max_pos, measured_v))

                    self.chosen_measurements[i] = max_pos
                    self.idx_loc = i
            self.prev_v = measured_v

        self.robot_system.all_anchor_positions = np.array(self.robot_system.all_anchor_positions)
        self.robot_system.all_target_positions = np.array(self.robot_system.all_target_positions)


def run_rotating_robot():
    p0 = [-9, -5]
    v0 = [1, 1]
    count = 400
    is_noisy = True
    rssi_noise = True
    r_std = 1.
    v_std = 0.1

    target_ax_std = .1
    target_ay_std = .1
    dt = .5

    target = RotatingRobot2D(init_pos=p0, init_vel=v0, dt=dt)
    system = TwoRobotSystem(None, target, is_noisy=is_noisy, r_std=r_std, v_std=v_std, rssi_noise=rssi_noise)

    # kf = filterpy.common.kinematic_kf(2, 1, dt, order_by_dim=False)
    kf = filterpy.kalman.KalmanFilter(4, 4)
    kf.F = np.array([[1.,  0.,  dt, 0.],
                     [0.,  1.,  0.,  dt],
                     [0.,  0.,  1.,  0.],
                     [0.,  0.,  0.,  1.]])

    ax_var = target_ax_std ** 2
    ay_var = target_ay_std ** 2
    kf.Q = np.array([[dt ** 4 * ax_var / 4, 0, dt ** 3 * ax_var / 2, 0],
                     [0, dt ** 4 * ay_var / 4, 0, dt ** 3 * ay_var / 2],
                     [dt ** 3 * ax_var / 2, 0, dt ** 2 * ax_var, 0],
                     [0, dt ** 3 * ay_var / 2, 0, dt ** 2 * ay_var]])

    # r_std = .1
    # kf.R = np.array([[38.43291417,  9.34092651], [9.34092651, 46.58549306]])

    # rssi noise
    # kf.R = np.array([[21.62548567,  0.99567984], [0.99567984, 21.36388223]])

    # r_std = 1.
    kf.R = np.array([[38.64475783, -0.72235203], [-0.72235203, 39.07971159]])

    kf.P = np.zeros((4, 4))
    kf.P[:2, :2] = copy.deepcopy(kf.R)
    kf.P[2, 2] = v_std
    kf.P[3, 3] = v_std
    kf.R = copy.deepcopy(kf.P)
    kf.H = np.eye(4)

    loc = MotionBasedLocalization(system, count, kf, known_initial_pos=False)
    loc.run()

    rmse_est = Util.rmse(loc.estimated_positions[loc.idx_loc:], system.all_target_positions[loc.idx_loc:])
    rmse_conv = Util.rmse(loc.estimated_positions[150:], system.all_target_positions[150:])
    trunc_rmse_est = ['%.4f' % val for val in rmse_est]
    title = f"Motion-based localization (unknown initial position), dt = {dt}, RMSE = {trunc_rmse_est}"
    if is_noisy:
        if rssi_noise:
            title += f", RSSI noise"
        else:
            title += f", $\sigma_r={r_std}$, $\sigma_v$ = {v_std}"
    rmse_meas = Util.rmse(loc.chosen_measurements[loc.idx_loc:], loc.robot_system.all_target_positions[loc.idx_loc:])
    print(f"RMSE_est = {rmse_est}, RMSE_meas = {rmse_meas}, RMSE_conv = {rmse_conv}")

    loc.animate_results(title=title, save=False, plot_error_figures=True)
    return rmse_conv


def run_motion_based_localization():
    p0 = [3., 2.]
    v0 = [1, 1]
    # u = [[1, 0]] * 100 + [[1, 2]] * 100 + [[-1, 0]] * 100

    # u = [[1., 0.]] * 20 + [[1., 2.]] * 20 + [[-2, 1]] * 300
    u = [[1., 0.]] * 50 + [[1., 2.]] * 20 + [[-2, 1]] * 50 + [[-1, -1]] * 100 + [[0, 2]] * 50

    # Shows that noise increases the further the robot gets
    # u = [[1., 0.]] * 25 + [[1., 2.]] * 25 + [[2, 1]] * 100 + [[0, 1]] * 1000 + [[1, 0]] * 100 +  [[0, -1]] * 1000

    # Shows that dr has a lot more impact on bigger r
    # u = [[1., 0.]] * 25 + [[1., 2.]] * 25 + [[1., -2.]] * 25 + [[1., 0.]] * 1000
    # u = [[1., -2.]] * 10 + [[-1., -2.]] * 10 + [[0., -1.]] * 1000

    count = len(u)
    dt = .5
    is_noisy = True
    r_std = .1
    v_std = 0
    target_ax_std = 1.5
    target_ay_std = 1.
    # target = ControlledRobot2D(u, dt=dt, init_pos=p0, init_vel=u[0])

    target = RandomAccelerationRobot2D(p0, v0, dt, ax_noise=target_ax_std, ay_noise=target_ay_std)
    # anchor = RandomAccelerationRobot2D([0., 0.], [-1, 1], dt, ax_noise=1, ay_noise=1.5)

    system = TwoRobotSystem(None, target, is_noisy=is_noisy, r_std=r_std, v_std=v_std)

    # kf = filterpy.common.kinematic_kf(2, 1, dt, order_by_dim=False)
    kf = filterpy.kalman.KalmanFilter(4, 4)
    kf.F = np.array([[1.,  0.,  dt, 0.],
                     [0.,  1.,  0.,  dt],
                     [0.,  0.,  1.,  0.],
                     [0.,  0.,  0.,  1.]])

    ax_var = target_ax_std ** 2
    ay_var = target_ay_std ** 2
    kf.Q = np.array([[dt ** 4 * ax_var / 4, 0, dt ** 3 * ax_var / 2, 0],
                     [0, dt ** 4 * ay_var / 4, 0, dt ** 3 * ay_var / 2],
                     [dt ** 3 * ax_var / 2, 0, dt ** 2 * ax_var, 0],
                     [0, dt ** 3 * ay_var / 2, 0, dt ** 2 * ay_var]])

    # for r_std = 0.01
    # kf.R = np.array([[23424.51598114,  9877.72176729], [9877.72176729,  5403.0224787]])
    # kf.R = np.array([[4.16272827, 2.71208042], [2.71208042, 4.93606463]])

    # for r_std = 0.1
    # kf.R = np.array([[2912.56428837, -1857.63133772], [-1857.63133772, 10249.59328298]])
    # kf.R = np.array([[256.27401329, 220.00956481], [220.00956481, 296.36179637]])
    # kf.R = np.array([[3.80227355, 2.00813648], [2.00813648, 4.85322846]])

    # Controlled robot r=.1, dt=.5
    # kf.R = np.array([[586.34069508, -194.27242372], [-194.27242372,  85.37535859]])

    # dt = .5,  r = 1
    # kf.R = np.array([[4849.90968636, -1508.56463433], [-1508.56463433,  2064.63924781]])

    # Controlled Robot r=.1, dt=1
    # kf.R = np.array([[48.85914417,  1.39985785], [1.39985785, 12.77227353]])
    # rssi noise
    kf.R = np.array([[21.62548567,  0.99567984], [0.99567984, 21.36388223]])

    kf.P = np.zeros((4, 4))
    kf.P[:2, :2] = copy.deepcopy(kf.R)
    kf.P[2, 2] = v_std
    kf.P[3, 3] = v_std
    kf.R = copy.deepcopy(kf.P)
    kf.H = np.eye(4)
    loc = MotionBasedLocalization(system, count, kf)
    loc.run()

    rmse_est = Util.rmse(loc.estimated_positions[loc.idx_loc:], system.all_target_positions[loc.idx_loc:])
    trunc_rmse_est = ['%.4f' % val for val in rmse_est]
    title = f"Motion-based localization (unknown initial position), dt = {dt}, RMSE = {trunc_rmse_est}"
    if is_noisy:
        title += f", $\sigma_r={r_std}$, $\sigma_v$ = {v_std}"
    rmse_meas = Util.rmse(loc.chosen_measurements[loc.idx_loc:], loc.robot_system.all_target_positions[loc.idx_loc:])
    print(f"RMSE_est = {rmse_est}, RMSE_meas = {rmse_meas}")

    loc.animate_results(title=title, save=False, plot_error_figures=True)


def determine_r_matrix():
    u = [[1., 0.]] * 25 + [[1., 2.]] * 25 + [[-2, 1]] * 25 + [[-1, -1]] * 50 + [[0, 2]] * 25
    count = 400
    p0 = [-9., -5.]
    v0 = [1, 1]
    dt = .5
    is_noisy = True
    r_std = 1.
    v_std = 0.1
    target_ax_std = 1.5
    target_ay_std = 1.0

    reps = 1000
    q = np.array([[0., 0.], [0., 0.]])
    for i in range(reps):
        # target = ControlledRobot2D(u, dt=dt, init_pos=p0, init_vel=u[0])
        target = RotatingRobot2D(dt=dt, init_pos=p0, init_vel=u[0])
        # target = RandomAccelerationRobot2D(p0, v0, dt, ax_noise=target_ax_std, ay_noise=target_ay_std)
        system = TwoRobotSystem(None, target, is_noisy=is_noisy, r_std=r_std, v_std=v_std)
        loc = MotionBasedLocalization(system, count)
        loc.run()

        if loc.localized:
            error = loc.chosen_measurements[loc.idx_loc:] - system.all_target_positions[loc.idx_loc:]
            e_nx = np.mean(error[:, 0]**2)
            e_nx_ny = np.mean(error[:, 0] * error[:, 1])
            e_ny = np.mean(error[:, 1]**2)
            q += np.array([[e_nx, e_nx_ny], [e_nx_ny, e_ny]])
        else:
            reps -= 1
    q /= reps
    print(reps)
    return q


if __name__ == '__main__':
    np.seterr(all='raise')
    # run_motion_based_localization()
    run_rotating_robot()
    # run_position_tracking()
    # print(determine_r_matrix())
