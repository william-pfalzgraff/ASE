import sys
import threading
import warnings
from abc import ABC, abstractmethod
import time

import numpy as np

from scipy.interpolate import CubicSpline
from scipy.integrate import cumtrapz
from scipy.optimize import root

import ase.parallel
from ase.build import minimize_rotation_and_translation
from ase.calculators.calculator import Calculator
from ase.calculators.singlepoint import SinglePointCalculator
from ase.io import read
from ase.optimize import MDMin
from ase.optimize.optimize import Optimizer
from ase.geometry import find_mic
from ase.optimize import MDMin
from ase.utils import lazyproperty, deprecated
from ase.utils.forcecurve import fit_images
from ase.optimize.precon import make_precon_images
from ase.optimize.ode import ode12r


class Spring:
    def __init__(self, atoms1, atoms2, energy1, energy2, k):
        self.atoms1 = atoms1
        self.atoms2 = atoms2
        self.energy1 = energy1
        self.energy2 = energy2
        self.k = k

    def _find_mic(self):
        pos1 = self.atoms1.get_positions()
        pos2 = self.atoms2.get_positions()
        # XXX If we want variable cells we will need to edit this.
        mic, _ = find_mic(pos2 - pos1, self.atoms1.cell, self.atoms1.pbc)
        return mic

    @lazyproperty
    def t(self):
        return self._find_mic()

    @lazyproperty
    def nt(self):
        return np.linalg.norm(self.t)


class NEBState:
    def __init__(self, neb, images, energies, precon):
        self.neb = neb
        self.images = images
        self.energies = energies
        self.precon = precon
            
    def spring(self, i):
        return Spring(self.images[i], self.images[i + 1],
                      self.energies[i], self.energies[i + 1],
                      self.neb.k[i])

    @lazyproperty
    def imax(self):
        return 1 + np.argsort(self.energies[1:-1])[-1]

    @property
    def emax(self):
        return self.energies[self.imax]

    @lazyproperty
    def eqlength(self):
        images = self.images
        beeline = (images[self.neb.nimages - 1].get_positions() -
                   images[0].get_positions())
        beelinelength = np.linalg.norm(beeline)
        return beelinelength / (self.neb.nimages - 1)

    @lazyproperty
    def nimages(self):
        return len(self.images)

    @lazyproperty
    def spline(self):
        return self.neb.spline_fit()


class NEBMethod(ABC):
    def __init__(self, neb):
        self.neb = neb

    @abstractmethod
    def get_tangent(self, state, spring1, spring2, i):
        ...

    @abstractmethod
    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        ...

    def adjust_positions(self):
        pass


class ImprovedTangentMethod(NEBMethod):
    """
    Tangent estimates are improved according to Eqs. 8-11 in paper I.
    Tangents are weighted at extrema to ensure smooth transitions between
    the positive and negative tangents.
    """

    def get_tangent(self, state, spring1, spring2, i):
        energies = state.energies
        if energies[i + 1] > energies[i] > energies[i - 1]:
            tangent = spring2.t.copy()
        elif energies[i + 1] < energies[i] < energies[i - 1]:
            tangent = spring1.t.copy()
        else:
            deltavmax = max(abs(energies[i + 1] - energies[i]),
                            abs(energies[i - 1] - energies[i]))
            deltavmin = min(abs(energies[i + 1] - energies[i]),
                            abs(energies[i - 1] - energies[i]))
            if energies[i + 1] > energies[i - 1]:
                tangent = spring2.t * deltavmax + spring1.t * deltavmin
            else:
                tangent = spring2.t * deltavmin + spring1.t * deltavmax
        # Normalize the tangent vector
        tangent /= np.linalg.norm(tangent)
        return tangent

    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        imgforce -= tangential_force * tangent
        # Improved parallel spring force (formula 12 of paper I)
        imgforce += (spring2.nt * spring2.k - spring1.nt * spring1.k) * tangent


class ASENEBMethod(NEBMethod):
    """
    Standard NEB implementation in ASE. The tangent of each image is
    estimated from the spring closest to the saddle point in each
    spring pair.
    """

    def get_tangent(self, state, spring1, spring2, i):
        imax = self.neb.imax
        if i < imax:
            tangent = spring2.t
        elif i > imax:
            tangent = spring1.t
        else:
            tangent = spring1.t + spring2.t
        return tangent

    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        tangent_mag = np.vdot(tangent, tangent)  # Magnitude for normalizing
        factor = tangent / tangent_mag
        imgforce -= tangential_force * factor
        imgforce -= np.vdot(
            spring1.t * spring1.k -
            spring2.t * spring2.k, tangent) * factor


class FullSpringMethod(NEBMethod):
    """
    Elastic band method. The full spring force is included.
    """
    
    def get_tangent(self, state, spring1, spring2, i):
        # Tangents are bisections of spring-directions
        # (formula C8 of paper III)
        tangent = spring1.t / spring1.nt + spring2.t / spring2.nt
        tangent /= np.linalg.norm(tangent)
        return tangent

    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        imgforce -= tangential_force * tangent
        energies = state.energies
        # Spring forces
        # Eqs. C1, C5, C6 and C7 in paper III)
        f1 = -(spring1.nt -
               state.eqlength) * spring1.t / spring1.nt * spring1.k
        f2 = (spring2.nt - state.eqlength) * spring2.t / spring2.nt * spring2.k
        if self.neb.climb and abs(i - self.neb.imax) == 1:
            deltavmax = max(abs(energies[i + 1] - energies[i]),
                            abs(energies[i - 1] - energies[i]))
            deltavmin = min(abs(energies[i + 1] - energies[i]),
                            abs(energies[i - 1] - energies[i]))
            imgforce += (f1 + f2) * deltavmin / deltavmax
        else:
            imgforce += f1 + f2


class BaseSplineMethod(NEBMethod):
    """
    Base class for SplineNEB and String methods

    Can optionally be preconditioned, as described in the following article:

        S. Makri, C. Ortner and J. R. Kermode, J. Chem. Phys.
        150, 094109 (2019)
        https://dx.doi.org/10.1063/1.5064465
    """
    def __init__(self, neb):
        NEBMethod.__init__(self, neb)
        self.residuals = np.zeros(neb.nimages)

    def get_tangent(self, state, spring1, spring2, i):
        reaction_coordinate, _, _, dx_ds, _ = state.spline
        tangent = dx_ds(reaction_coordinate[i])
        tangent /= state.precon[i].norm(tangent)
        return tangent.reshape(-1, 3)

    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        # update preconditioner and apply to image force
        precon_imgforce, _ = state.precon[i].apply(imgforce.reshape(-1),
                                                   state.images[i])
        imgforce[:] = precon_imgforce.reshape(-1, 3)
        
        # project out tangential component (Eqs 6 and 7 in Paper IV)
        imgforce -= tangential_force * tangent

        # Store residuals for each image (Eq. 11)
        P_dot_imgforce = state.precon[i].Pdot(imgforce.reshape(-1))
        self.residuals[i - 1] = np.linalg.norm(P_dot_imgforce, np.inf)

    def get_residual(self):
        return np.max(self.residuals)  # Eq. 11


class SplineMethod(BaseSplineMethod):
    """
    NEB using spline interpolation, plus optional preconditioning
    """
    def add_image_force(self, state, tangential_force, tangent, imgforce,
                        spring1, spring2, i):
        super().add_image_force(state, tangential_force,
                                tangent, imgforce, spring1, spring2, i)
        
        reaction_coordinate, _, x, dx, d2x_ds2 = state.spline

        # Definition following Eq. 9 in Paper IV
        k = 0.5 * (spring1.k + spring2.k) / (state.nimages ** 2)
        curvature = d2x_ds2(reaction_coordinate[i]).reshape(-1, 3)
        eta = k * state.precon[i].vdot(curvature, tangent) * tangent

        # complete Eq. 9 by including the spring force
        imgforce += eta


class StringMethod(BaseSplineMethod):
    """
    String method using spline interpolation, plus optional preconditioning
    """
    def adjust_positions(self):
        # fit cubic spline to positions, reinterpolate to equispace images
        # note this use the precondionted distance metric if state.
        s, _, x, _, _ = self.neb.spline_fit()
        new_s = np.linspace(0.0, 1.0, self.neb.nimages)
        new_positions = x(new_s[1:-1]).reshape(-1, 3)
        self.neb.set_positions(new_positions)


def get_neb_method(neb, method):
    if method == 'eb':
        return FullSpringMethod(neb)
    elif method == 'aseneb':
        return ASENEBMethod(neb)
    elif method == 'improvedtangent':
        return ImprovedTangentMethod(neb)
    elif method == 'spline':
        return SplineMethod(neb)
    elif method == 'string':
        return StringMethod(neb)
    else:
        raise ValueError(f'Bad method: {method}')


class BaseNEB:
    def __init__(self, images, k=0.1, climb=False, parallel=False,
                 remove_rotation_and_translation=False, world=None,
                 method='aseneb', allow_shared_calculator=False, precon=None):
        self.climb = climb
            if len(img) != self.natoms:
                raise ValueError('Images have different numbers of atoms')
            if np.any(img.pbc != images[0].pbc):
                raise ValueError('Images have different boundary conditions')
            if np.any(img.get_atomic_numbers() !=
                      images[0].get_atomic_numbers()):
                raise ValueError('Images have atoms in different orders')
            if np.any(np.abs(img.get_cell() - images[0].get_cell()) > 1e-8):
                raise NotImplementedError("Variable cell NEB is not "
                                          "implemented yet")

        self.emax = np.nan

        self.remove_rotation_and_translation = remove_rotation_and_translation

        if method in ['aseneb', 'eb', 'improvedtangent', 'spline', 'string']:
            self.method = method
        else:
            raise NotImplementedError(method)

        if method in ['spline', 'string']:
            precon = make_precon_images(precon, images)
        else:
            if precon is not None:
                raise NotImplementedError(f'no precon implemented: {method}')
        self.precon = precon

        self.neb_method = get_neb_method(self, method)
        if isinstance(k, (float, int)):
        self.k = list(k)

        if world is None:
            world = ase.parallel.world
        self.world = world

        if parallel:
            if self.allow_shared_calculator:
                raise RuntimeError(
                    "Cannot use shared calculators in parallel in NEB.")
        self.real_forces = None  # ndarray of shape (nimages, natom, 3)
        self.energies = None  # ndarray of shape (nimages,)

    def natoms(self):
        return len(self.images[0])

    @property
    def nimages(self):
        return len(self.images)

    def freeze_results_on_image(atoms: ase.Atoms,
                                **results_to_include):
        atoms.calc = SinglePointCalculator(atoms=atoms, **results_to_include)

    def interpolate(self, method='linear', mic=False):
        """Interpolate the positions of the interior images between the
        initial state (image 0) and final state (image -1).

        method: str
            Method by which to interpolate: 'linear' or 'idpp'.
            linear provides a standard straight-line interpolation, while
            idpp uses an image-dependent pair potential.
        mic: bool
            Use the minimum-image convention when interpolating.
        """
        if self.remove_rotation_and_translation:
            minimize_rotation_and_translation(self.images[0], self.images[-1])

        interpolate(self.images, mic)

        if method == 'idpp':
            idpp_interpolate(images=self, traj=None, log=None, mic=mic)

    @deprecated("Please use NEB's interpolate(method='idpp') method or "
                "directly call the idpp_interpolate function from ase.neb")
    def idpp_interpolate(self, traj='idpp.traj', log='idpp.log', fmax=0.1,
                         optimizer=MDMin, mic=False, steps=100):
        idpp_interpolate(self, traj=traj, log=log, fmax=fmax,
                         optimizer=optimizer, mic=mic, steps=steps)

    def get_positions(self):
        positions = np.empty(((self.nimages - 2) * self.natoms, 3))
        n1 = 0
        for image in self.images[1:-1]:
            n2 = n1 + self.natoms
            positions[n1:n2] = image.get_positions()
            n1 = n2
        return positions

    def set_positions(self, positions):
        n1 = 0
        for image in self.images[1:-1]:
            n2 = n1 + self.natoms
            image.set_positions(positions[n1:n2])
            n1 = n2

    def adjust_positions(self):
        # allow the NEB method to reparameterise images if necessary
        self.neb_method.adjust_positions()

    def get_forces(self):
        """Evaluate and return the forces."""
        images = self.images

        if not self.allow_shared_calculator:
            calculators = [image.calc for image in images
                           if image.calc is not None]
            if len(set(calculators)) != len(calculators):
                msg = ('One or more NEB images share the same calculator.  '
                       'Each image must have its own calculator.  '
                       'You may wish to use the ase.neb.SingleCalculatorNEB '
                       'class instead, although using separate calculators '
                       'is recommended.')
                raise ValueError(msg)

        forces = np.empty(((self.nimages - 2), self.natoms, 3))
        energies = np.empty(self.nimages)

        if self.remove_rotation_and_translation:
            for i in range(1, self.nimages):
                minimize_rotation_and_translation(images[i - 1], images[i])

        if self.method != 'aseneb':
            energies[0] = images[0].get_potential_energy()
            energies[-1] = images[-1].get_potential_energy()

        if not self.parallel:
            # Do all images - one at a time:
            for i in range(1, self.nimages - 1):
                energies[i] = images[i].get_potential_energy()
                forces[i - 1] = images[i].get_forces()

        elif self.world.size == 1:
            def run(image, energies, forces):
                energies[:] = image.get_potential_energy()
                forces[:] = image.get_forces()

            threads = [threading.Thread(target=run,
                                        args=(images[i],
                                              energies[i:i + 1],
                                              forces[i - 1:i]))
                       for i in range(1, self.nimages - 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        else:
            # Parallelize over images:
            i = self.world.rank * (self.nimages - 2) // self.world.size + 1
            try:
                energies[i] = images[i].get_potential_energy()
                forces[i - 1] = images[i].get_forces()
            except Exception:
                # Make sure other images also fail:
                error = self.world.sum(1.0)
                raise
            else:
                error = self.world.sum(0.0)
                if error:
                    raise RuntimeError('Parallel NEB failed!')

            for i in range(1, self.nimages - 1):
                root = (i - 1) * self.world.size // (self.nimages - 2)
                self.world.broadcast(energies[i:i + 1], root)
                self.world.broadcast(forces[i - 1], root)

        # Save for later use in iterimages:
        self.energies = energies
        self.real_forces = np.zeros((self.nimages, self.natoms, 3))
        self.real_forces[1:-1] = forces

        state = NEBState(self, images, energies, self.precon)

        # Can we get rid of self.energies, self.imax, self.emax etc.?
        self.imax = state.imax
        self.emax = state.emax

        spring1 = state.spring(0)

        for i in range(1, self.nimages - 1):
            spring2 = state.spring(i)
            tangent = self.neb_method.get_tangent(state, spring1, spring2, i)

            imgforce = forces[i - 1]
            # Get overlap between PES-derived force and tangent
            tangential_force = np.vdot(imgforce, tangent)

            if i == self.imax and self.climb:
                """The climbing image, imax, is not affected by the spring
                   forces. This image feels the full PES-derived force,
                   but the tangential component is inverted:
                   see Eq. 5 in paper II."""
                if self.method == 'aseneb':
                    tangent_mag = np.vdot(tangent, tangent)  # For normalizing
                    imgforce -= 2 * tangential_force / tangent_mag * tangent
                else:
                    imgforce -= 2 * tangential_force * tangent
            else:
                self.neb_method.add_image_force(state, tangential_force,
                                                tangent, imgforce, spring1,
                                                spring2, i)

            spring1 = spring2
        return forces.reshape((-1, 3))

    def get_residual(self):
        """Return residual force along the band.

        Typically this the maximum force component, differing
        only for preconditioned cases.
        """
        if self.method == 'spline' or self.method == 'string':
            return self.neb_method.get_residual()
        else:
            return np.max(self.get_forces())

    def get_potential_energy(self, force_consistent=False):
        """Return the maximum potential energy along the band.
        Note that the force_consistent keyword is ignored and is only
        present for compatibility with ase.Atoms.get_potential_energy."""
        return self.emax

    def set_calculators(self, calculators):
        """Set new calculators to the images.

        Parameters
        ----------
        calculators : Calculator / list(Calculator)
            calculator(s) to attach to images
              - single calculator, only if allow_shared_calculator=True
            list of calculators if length:
              - length nimages, set to all images
              - length nimages-2, set to non-end images only
        """

        if not isinstance(calculators, list):
            if self.allow_shared_calculator:
                calculators = [calculators] * self.nimages
            else:
                raise RuntimeError("Cannot set shared calculator to NEB "
                                   "with allow_shared_calculator=False")

        n = len(calculators)
        if n == self.nimages:
            for i in range(self.nimages):
                self.images[i].calc = calculators[i]
        elif n == self.nimages - 2:
            for i in range(1, self.nimages - 1):
                self.images[i].calc = calculators[i - 1]
        else:
            raise RuntimeError(
                'len(calculators)=%d does not fit to len(images)=%d'
                % (n, self.nimages))

    def __len__(self):
        # Corresponds to number of optimizable degrees of freedom, i.e.
        # virtual atom count for the optimization algorithm.
        return (self.nimages - 2) * self.natoms

    def iterimages(self):
        # Allows trajectory to convert NEB into several images
        for i, atoms in enumerate(self.images):
            if i == 0 or i == self.nimages - 1:
                yield atoms
            else:
                atoms = atoms.copy()
                self.freeze_results_on_image(
                    atoms, energy=self.energies[i],
                    forces=self.real_forces[i])

                yield atoms

    def spline_fit(self, norm='precon'):
        """
        Return spline fit to images, as described in paper IV

        Returns
        -------
            s - reaction coordinate
            x - displacement values
            x_spline - spline fit to x
            dx_ds_spline - derivative of spline fit
            d2x_ds2_spline - 2nd derivative of spline fit
        """

        images = self.images
        d_P = np.zeros(self.nimages)
        x = np.zeros((self.nimages, 3 * self.natoms))  # flattened positions
        x[0, :] = images[0].positions.reshape(-1)

        for i in range(1, self.nimages):
            x[i, :] = images[i].positions.reshape(-1)
            dx, _ = find_mic(images[i].positions -
                             images[i - 1].positions,
                             images[i - 1].cell,
                             images[i - 1].pbc)
            dx = dx.reshape(-1)

            # distance as defined in Eq. 8 in paper IV
            if norm == 'euclidean':
                d_P[i] = np.linalg.norm(dx)
            elif norm == 'precon':
                d_P[i] = np.sqrt(0.5 * (self.precon[i].dot(dx, dx) +
                                        self.precon[i - 1].dot(dx, dx)))
            else:
                raise ValueError(f'unknown norm {norm} in spline_fit()')

        s = d_P.cumsum() / d_P.sum()  # Eq. A1 in paper IV
        
        x_spline = CubicSpline(s, x, bc_type='not-a-knot')
        dx_ds_spline = x_spline.derivative()
        d2x_ds2_spline = x_spline.derivative(2)
       
        return s, x, x_spline, dx_ds_spline, d2x_ds2_spline
    
    def integrate_forces(self, spline_points=1000, bc_type='not-a-knot',
                         return_forces=False):
        """
        Use spline fit to integrate forces along MEP to approximate
        energy differences using the virtual work approach.

        Parameters
        ----------
        spline_points - number of spline points to use
        return_forces - if True, include forces in results as well as energies

        Returns
        -------

        s - reaction coordinate in range [0, 1], with `spline_points` entries
        E - result of integrating forces, on the same grid as `s`.
        F - if return_forces is True, also return projected forces along MEP
        """
        # note we use standard Euclidean rather than preconditioned norm
        # to compute the virtual work
        s, _, x, dx, _ = self.spline_fit(norm='euclidean')
        forces = np.array([image.get_forces().reshape(-1)
                           for image in self.images])
        f = CubicSpline(s, forces, bc_type=bc_type)

        s = np.linspace(0.0, 1.0, spline_points, endpoint=True)
        dE = f(s) * dx(s)
        F = dE.sum(axis=1)
        E = -cumtrapz(F, s, initial=0.0)
        if return_forces:
            return s, E, F
        else:
            return s, E


class DyNEB(BaseNEB):
    def __init__(self, images, k=0.1, fmax=0.05, climb=False, parallel=False,
                 remove_rotation_and_translation=False, world=None,
                 dynamic_relaxation=True, scale_fmax=0., method='aseneb',
                 allow_shared_calculator=False, precon=None):
        """
        Subclass of NEB that allows for scaled and dynamic optimizations of
        images. This method, which only works in series, does not perform
        force calls on images that are below the convergence criterion.
        The convergence criteria can be scaled with a displacement metric
        to focus the optimization on the saddle point region.

        'Scaled and Dynamic Optimizations of Nudged Elastic Bands',
        P. Lindgren, G. Kastlunger and A. A. Peterson,
        J. Chem. Theory Comput. 15, 11, 5787-5793 (2019).

        dynamic_relaxation: bool
            True skips images with forces below the convergence criterion.
            This is updated after each force call; if a previously converged
            image goes out of tolerance (due to spring adjustments between
            the image and its neighbors), it will be optimized again.
            False reverts to the default NEB implementation.

        fmax: float
            Must be identical to the fmax of the optimizer.

        scale_fmax: float
            Scale convergence criteria along band based on the distance between
            an image and the image with the highest potential energy. This
            keyword determines how rapidly the convergence criteria are scaled.
        """
        super().__init__(
            images, k=k, climb=climb, parallel=parallel,
            remove_rotation_and_translation=remove_rotation_and_translation,
            world=world, method=method,
            allow_shared_calculator=allow_shared_calculator, precon=precon)
        self.fmax = fmax
        self.dynamic_relaxation = dynamic_relaxation
        self.scale_fmax = scale_fmax

        if not self.dynamic_relaxation and self.scale_fmax:
            msg = ('Scaled convergence criteria only implemented in series '
                   'with dynamic relaxation.')
            raise ValueError(msg)

    def set_positions(self, positions):
        if not self.dynamic_relaxation:
            return super().set_positions(positions)

        n1 = 0
        for i, image in enumerate(self.images[1:-1]):
            if self.parallel:
                msg = ('Dynamic relaxation does not work efficiently '
                       'when parallelizing over images. Try AutoNEB '
                       'routine for freezing images in parallel.')
                raise ValueError(msg)
            else:
                forces_dyn = self._fmax_all(self.images)
                if forces_dyn[i] < self.fmax:
                    n1 += self.natoms
                else:
                    n2 = n1 + self.natoms
                    image.set_positions(positions[n1:n2])
                    n1 = n2

    def _fmax_all(self, images):
        """Store maximum force acting on each image in list. This is used in
           the dynamic optimization routine in the set_positions() function."""
        n = self.natoms
        forces = self.get_forces()
        fmax_images = [
            np.sqrt((forces[n * i:n + n * i] ** 2).sum(axis=1)).max()
            for i in range(self.nimages - 2)]
        return fmax_images

    def get_forces(self):
        forces = super().get_forces()
        if not self.dynamic_relaxation:
            return forces

        """Get NEB forces and scale the convergence criteria to focus
           optimization on saddle point region. The keyword scale_fmax
           determines the rate of convergence scaling."""
        n = self.natoms
        for i in range(self.nimages - 2):
            n1 = n * i
            n2 = n1 + n
            force = np.sqrt((forces[n1:n2] ** 2.).sum(axis=1)).max()
            n_imax = (self.imax - 1) * n  # Image with highest energy.

            positions = self.get_positions()
            pos_imax = positions[n_imax:n_imax + n]

            """Scale convergence criteria based on distance between an
               image and the image with the highest potential energy."""
            rel_pos = np.sqrt(((positions[n1:n2] - pos_imax) ** 2).sum())
            if force < self.fmax * (1 + rel_pos * self.scale_fmax):
                if i == self.imax - 1:
                    # Keep forces at saddle point for the log file.
                    pass
                else:
                    # Set forces to zero before they are sent to optimizer.
                    forces[n1:n2, :] = 0
        return forces


def _check_deprecation(keyword, kwargs):
    if keyword in kwargs:
        warnings.warn(f'Keyword {keyword} of NEB is deprecated.  '
                      'Please use the DyNEB class instead for dynamic '
                      'relaxation', FutureWarning)


class NEB(DyNEB):
    def __init__(self, images, k=0.1, climb=False, parallel=False,
                 remove_rotation_and_translation=False, world=None,
                 method='aseneb', allow_shared_calculator=False, precon=precon, **kwargs):
        """Nudged elastic band.

        Paper I:

            G. Henkelman and H. Jonsson, Chem. Phys, 113, 9978 (2000).
            https://doi.org/10.1063/1.1323224

        Paper II:

            G. Henkelman, B. P. Uberuaga, and H. Jonsson, Chem. Phys,
            113, 9901 (2000).
            https://doi.org/10.1063/1.1329672

        Paper III:

            E. L. Kolsbjerg, M. N. Groves, and B. Hammer, J. Chem. Phys,
            145, 094107 (2016)
            https://doi.org/10.1063/1.4961868

        Paper IV:

            S. Makri, C. Ortner and J. R. Kermode, J. Chem. Phys.
            150, 094109 (2019)
            https://dx.doi.org/10.1063/1.5064465

        images: list of Atoms objects
            Images defining path from initial to final state.
        k: float or list of floats
            Spring constant(s) in eV/Ang.  One number or one for each spring.
        climb: bool
            Use a climbing image (default is no climbing image).
        parallel: bool
            Distribute images over processors.
        remove_rotation_and_translation: bool
            TRUE actives NEB-TR for removing translation and
            rotation during NEB. By default applied non-periodic
            systems
        method: string of method
            Choice betweeen five methods:

            * aseneb: standard ase NEB implementation
            * improvedtangent: Paper I NEB implementation
            * eb: Paper III full spring force implementation
            * spline: Paper IV spline interpolation (supports precon)
            * string: Paper IV string method (supports precon)
        allow_shared_calculator: bool
            Allow images to share the same calculator between them.
            Incompatible with parallelisation over images.
        precon: string, ase.optimize.precon.Precon instance or list of instances
            If present, enable preconditioing as in Paper IV. This is
            possible using the 'spline' or 'string' methods.
            Default is no preconditioning (precon=None)
        """
        for keyword in 'dynamic_relaxation', 'fmax', 'scale_fmax':
            _check_deprecation(keyword, kwargs)
        defaults = dict(dynamic_relaxation=False,
                        fmax=0.05,
                        scale_fmax=0.0)
        defaults.update(kwargs)
        # Only reason for separating BaseNEB/NEB is that we are
        # deprecating dynamic_relaxation.
        #
        # We can turn BaseNEB into NEB once we get rid of the
        # deprecated variables.
        #
        # Then we can also move DyNEB into ase.dyneb without cyclic imports.
        # We can do that in ase-3.22 or 3.23.
        super().__init__(
            images, k=k, climb=climb, parallel=parallel,
            remove_rotation_and_translation=remove_rotation_and_translation,
            world=world, method=method,
            allow_shared_calculator=allow_shared_calculator,
            precon=precon,
            **defaults)


class NEBOptimizer(Optimizer):
    """
    This optimizer applies an adaptive ODE or Krylov solver to a NEB

    Details of the adaptive ODE solver are descried in paper IV
    """
    def __init__(self,
                 neb,
                 restart=None, logfile='-', trajectory=None,
                 master=None,
                 append_trajectory=False,
                 method='ODE',
                 alpha=0.01,
                 verbose=0,
                 rtol=0.1,
                 C1=1e-2,
                 C2=2.0):

        Optimizer.__init__(self, None, restart, logfile, trajectory,
                           master=master,
                           append_trajectory=append_trajectory,
                           force_consistent=False)
        self.neb = neb

        method = method.lower()
        methods = ['ode', 'static', 'krylov']
        if method not in methods:
            raise ValueError(f'method must be one of {methods}')
        self.method = method

        self.alpha = alpha
        self.verbose = verbose
        self.rtol = rtol
        self.C1 = C1
        self.C2 = C2

        self.fmax_history = []

    def get_dofs(self):
        return self.neb.get_positions().reshape(-1)

    def set_dofs(self, X):
        self.neb.set_positions(X.reshape((self.neb.nimages - 2) * 
                                         self.neb.natoms, 3))

    def force_function(self, X):
        self.set_dofs(X)
        return self.neb.get_forces().reshape(-1)

    def get_residual(self, F=None, X=None):
        return self.neb.get_residual()

    def log(self):
        fmax = self.get_residual()
        self.fmax_history.append(fmax)
        T = time.localtime()
        if self.logfile is not None:
            name = f'{self.__class__.__name__}[{self.method}]'
            if self.nsteps == 0:
                args = (" " * len(name), "Step", "Time", "fmax")
                msg = "%s  %4s %8s %12s\n" % args
                self.logfile.write(msg)

            args = (name, self.nsteps, T[3], T[4], T[5], fmax)
            msg = "%s:  %3d %02d:%02d:%02d %12.4f\n" % args
            self.logfile.write(msg)
            self.logfile.flush()

    def callback(self, X, F=None):
        self.log()
        self.call_observers()
        self.nsteps += 1
        self.neb.adjust_positions()

    def run(self, fmax=1e-3, steps=50):
        """
        Optimize images to obtain the minimum energy path

        Parameters
        ----------
        fmax - desired force tolerance
        steps - maximum number of steps
        """

        if self.method == 'ode':
            ode12r(self.force_function,
                   self.get_dofs(),
                   fmax=fmax,
                   rtol=self.rtol,
                   C1=self.C1,
                   C2=self.C2,
                   steps=steps,
                   verbose=self.verbose,
                   callback=self.callback,
                   residual=self.get_residual)
        elif self.method == 'krylov':
            res = root(self.force_function,
                       self.get_dofs(),
                       method='krylov',
                       options={'disp': True, 'fatol': fmax, 'maxiter': steps},
                       callback=self.callback)
            if res.success:
                self.set_dofs(res.x)
            else:
                raise RuntimeError(f'Krylov did not converge in {steps} steps')
        else:
            X = self.get_dofs()
            for step in range(steps):
                F = self.neb.force_function(X)
                if self.neb.get_residual() <= fmax:
                    break
                X += self.alpha * F
                self.callback(X)


class IDPP(Calculator):
    """Image dependent pair potential.

    See:
        Improved initial guess for minimum energy path calculations.
        Søren Smidstrup, Andreas Pedersen, Kurt Stokbro and Hannes Jónsson
        Chem. Phys. 140, 214106 (2014)
    """

    implemented_properties = ['energy', 'forces']

    def __init__(self, target, mic):
        Calculator.__init__(self)
        self.target = target
        self.mic = mic

    def calculate(self, atoms, properties, system_changes):
        Calculator.calculate(self, atoms, properties, system_changes)

        P = atoms.get_positions()
        d = []
        D = []
        for p in P:
            Di = P - p
            if self.mic:
                Di, di = find_mic(Di, atoms.get_cell(), atoms.get_pbc())
            else:
                di = np.sqrt((Di ** 2).sum(1))
            d.append(di)
            D.append(Di)
        d = np.array(d)
        D = np.array(D)

        dd = d - self.target
        d.ravel()[::len(d) + 1] = 1  # avoid dividing by zero
        d4 = d ** 4
        e = 0.5 * (dd ** 2 / d4).sum()
        f = -2 * ((dd * (1 - 2 * dd / d) / d ** 5)[..., np.newaxis] * D).sum(
            0)
        self.results = {'energy': e, 'forces': f}


@deprecated("SingleCalculatorNEB is deprecated. "
            "Please use NEB(allow_shared_calculator=True) instead.")
class SingleCalculatorNEB(NEB):
    def __init__(self, images, *args, **kwargs):
        kwargs["allow_shared_calculator"] = True
        super().__init__(images, *args, **kwargs)


def interpolate(images, mic=False, interpolate_cell=False,
                use_scaled_coord=False):
    """Given a list of images, linearly interpolate the positions of the
    interior images.

    mic: bool
         Map movement into the unit cell by using the minimum image convention.
    interpolate_cell: bool
         Interpolate the three cell vectors linearly just like the atomic
         positions. Not implemented for NEB calculations!
    use_scaled_coord: bool
         Use scaled/internal/fractional coordinates instead of real ones for the
         interpolation. Not implemented for NEB calculations!
    """
    if use_scaled_coord:
        pos1 = images[0].get_scaled_positions(wrap=mic)
        pos2 = images[-1].get_scaled_positions(wrap=mic)
    else:
        pos1 = images[0].get_positions()
        pos2 = images[-1].get_positions()
    d = pos2 - pos1
    if not use_scaled_coord and mic:
        d = find_mic(d, images[0].get_cell(), images[0].pbc)[0]
    d /= (len(images) - 1.0)
    if interpolate_cell:
        cell1 = images[0].get_cell()
        cell2 = images[-1].get_cell()
        cell_diff = cell2 - cell1
        cell_diff /= (len(images) - 1.0)
    for i in range(1, len(images) - 1):
        # first the new cell, otherwise scaled positions are wrong
        if interpolate_cell:
            images[i].set_cell(cell1 + i * cell_diff)
        new_pos = pos1 + i * d
        if use_scaled_coord:
            images[i].set_scaled_positions(new_pos)
        else:
            images[i].set_positions(new_pos)


def idpp_interpolate(images, traj='idpp.traj', log='idpp.log', fmax=0.1,
                     optimizer=MDMin, mic=False, steps=100):
    """Interpolate using the IDPP method. 'images' can either be a plain
    list of images or an NEB object (containing a list of images)."""
    if hasattr(images, 'interpolate'):
        neb = images
    else:
        neb = NEB(images)
    d1 = neb.images[0].get_all_distances(mic=mic)
    d2 = neb.images[-1].get_all_distances(mic=mic)
    d = (d2 - d1) / (neb.nimages - 1)
    real_calcs = []
    for i, image in enumerate(neb.images):
        real_calcs.append(image.calc)
        image.calc = IDPP(d1 + i * d, mic=mic)
    opt = optimizer(neb, trajectory=traj, logfile=log)
    opt.run(fmax=fmax, steps=steps)
    for image, calc in zip(neb.images, real_calcs):
        image.calc = calc


class NEBTools:
    """Class to make many of the common tools for NEB analysis available to
    the user. Useful for scripting the output of many jobs. Initialize with
    list of images which make up one or more band of the NEB relaxation."""

    def __init__(self, images):
        self.images = images

    @deprecated('NEBTools.get_fit() is deprecated.  '
                'Please use ase.utils.forcecurve.fit_images(images).')
    def get_fit(self):
        return fit_images(self.images)

    def get_barrier(self, fit=True, raw=False):
        """Returns the barrier estimate from the NEB, along with the
        Delta E of the elementary reaction. If fit=True, the barrier is
        estimated based on the interpolated fit to the images; if
        fit=False, the barrier is taken as the maximum-energy image
        without interpolation. Set raw=True to get the raw energy of the
        transition state instead of the forward barrier."""
        forcefit = fit_images(self.images)
        energies = forcefit.energies
        fit_energies = forcefit.fit_energies
        dE = energies[-1] - energies[0]
        if fit:
            barrier = max(fit_energies)
        else:
            barrier = max(energies)
        if raw:
            barrier += self.images[0].get_potential_energy()
        return barrier, dE

    def get_fmax(self, **kwargs):
        """Returns fmax, as used by optimizers with NEB."""
        neb = NEB(self.images, **kwargs)
        forces = neb.get_forces()
        return np.sqrt((forces ** 2).sum(axis=1).max())

    def plot_band(self, ax=None):
        """Plots the NEB band on matplotlib axes object 'ax'. If ax=None
        returns a new figure object."""
        forcefit = fit_images(self.images)
        ax = forcefit.plot(ax=ax)
        return ax.figure

    def plot_bands(self, constant_x=False, constant_y=False,
                   nimages=None, label='nebplots'):
        """Given a trajectory containing many steps of a NEB, makes
        plots of each band in the series in a single PDF.

        constant_x: bool
            Use the same x limits on all plots.
        constant_y: bool
            Use the same y limits on all plots.
        nimages: int
            Number of images per band. Guessed if not supplied.
        label: str
            Name for the output file. .pdf will be appended.
        """
        from matplotlib import pyplot
        from matplotlib.backends.backend_pdf import PdfPages
        if nimages is None:
            nimages = self._guess_nimages()
        nebsteps = len(self.images) // nimages
        if constant_x or constant_y:
            sys.stdout.write('Scaling axes.\n')
            sys.stdout.flush()
            # Plot all to one plot, then pull its x and y range.
            fig, ax = pyplot.subplots()
            for index in range(nebsteps):
                images = self.images[index * nimages:(index + 1) * nimages]
                NEBTools(images).plot_band(ax=ax)
                xlim = ax.get_xlim()
                ylim = ax.get_ylim()
            pyplot.close(fig)  # Reference counting "bug" in pyplot.
        with PdfPages(label + '.pdf') as pdf:
            for index in range(nebsteps):
                sys.stdout.write('\rProcessing band {:10d} / {:10d}'
                                 .format(index, nebsteps))
                sys.stdout.flush()
                fig, ax = pyplot.subplots()
                images = self.images[index * nimages:(index + 1) * nimages]
                NEBTools(images).plot_band(ax=ax)
                if constant_x:
                    ax.set_xlim(xlim)
                if constant_y:
                    ax.set_ylim(ylim)
                pdf.savefig(fig)
                pyplot.close(fig)  # Reference counting "bug" in pyplot.
        sys.stdout.write('\n')

    def _guess_nimages(self):
        """Attempts to guess the number of images per band from
        a trajectory, based solely on the repetition of the
        potential energy of images. This should also work for symmetric
        cases."""
        e_first = self.images[0].get_potential_energy()
        nimages = None
        for index, image in enumerate(self.images[1:], start=1):
            e = image.get_potential_energy()
            if e == e_first:
                # Need to check for symmetric case when e_first = e_last.
                try:
                    e_next = self.images[index + 1].get_potential_energy()
                except IndexError:
                    pass
                else:
                    if e_next == e_first:
                        nimages = index + 1  # Symmetric
                        break
                nimages = index  # Normal
                break
        if nimages is None:
            sys.stdout.write('Appears to be only one band in the images.\n')
            return len(self.images)
        # Sanity check that the energies of the last images line up too.
        e_last = self.images[nimages - 1].get_potential_energy()
        e_nextlast = self.images[2 * nimages - 1].get_potential_energy()
        if not (e_last == e_nextlast):
            raise RuntimeError('Could not guess number of images per band.')
        sys.stdout.write('Number of images per band guessed to be {:d}.\n'
                         .format(nimages))
        return nimages


class NEBtools(NEBTools):
    @deprecated('NEBtools has been renamed; please use NEBTools.')
    def __init__(self, images):
        NEBTools.__init__(self, images)


@deprecated('Please use NEBTools.plot_band_from_fit.')
def plot_band_from_fit(s, E, Sfit, Efit, lines, ax=None):
    NEBTools.plot_band_from_fit(s, E, Sfit, Efit, lines, ax=None)


def fit0(*args, **kwargs):
    raise DeprecationWarning('fit0 is deprecated. Use `fit_raw` from '
                             '`ase.utils.forcecurve` instead.')
