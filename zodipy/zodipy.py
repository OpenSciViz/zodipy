from typing import Literal, Optional, Sequence, Union, Tuple

import astropy.units as u
from astropy.units import Quantity
from astropy.time import Time
from astropy.coordinates import (
    get_body,
    HeliocentricMeanEcliptic,
    solar_system_ephemeris,
)
import healpy as hp
import numpy as np
from numpy.typing import NDArray

from zodipy._integral import trapezoidal
from zodipy._los_config import integration_config_registry
from zodipy._interp import interp_phase_coeffs, interp_comp_spectral_params
from zodipy._model import InterplanetaryDustModel
from zodipy.models import model_registry
from zodipy._labels import CompLabel


__all__ = ("Zodipy",)

# L2 not included in the astropy ephem so we manually include support for it
# by assuming its located at a constant distance radially from the Earth.
ADDITIONAL_SUPPORTED_OBSERVERS = ["semb-l2"]
DISTANCE_FROM_EARTH_TO_L2 = 0.009896235034000056 * u.AU


class Zodipy:
    """The Zodipy interface.

    Zodipy allows the user to produce simulated timestreams or binned maps of
    Interplanetary Dust emission as predicted by the Kelsall et al. (1998) model.
    """

    def __init__(self, model: str = "DIRBE", ephemeris: str = "de432s") -> None:
        """Initializes the interface given a model and an emphemeris.

        Parameters
        ----------
        model
            The name of the model to initialize. Defaults to DIRBE. See all
            available models in `zodipy.MODELS`.
        ephemeris
            Ephemeris used to compute the positions of the observer and Earth.
            Defaults to 'de432s' which requires downloading a 10 MB file.
            See https://docs.astropy.org/en/stable/coordinates/solarsystem.html
            for more information on available ephemeridises.
        """

        self._model = model_registry.get_model(model)
        self._line_of_sights = integration_config_registry.get_config()
        solar_system_ephemeris.set(ephemeris)

    @property
    def ipd_model(self) -> InterplanetaryDustModel:
        """Returns the IPD model."""

        return self._model

    @property
    def info(self) -> Optional[str]:
        return self._model.meta.get("info")

    @property
    def observers(self) -> Tuple[str, ...]:
        """Returns all observers suported by the ephemeridis."""

        observers = list(solar_system_ephemeris.bodies) + ADDITIONAL_SUPPORTED_OBSERVERS

        return tuple(observers)

    @u.quantity_input
    def get_emission(
        self,
        freq: Union[Quantity[u.Hz], Quantity[u.m]],
        obs_time: Time,
        obs: str = "earth",
        *,
        obs_pos: Optional[Quantity[u.AU]] = None,
        pixels: Optional[Union[int, Sequence[int], NDArray[np.int_]]] = None,
        theta: Optional[Union[Quantity[u.rad], Quantity[u.deg]]] = None,
        phi: Optional[Union[Quantity[u.rad], Quantity[u.deg]]] = None,
        nside: Optional[int] = None,
        lonlat: bool = False,
        binned: bool = False,
        return_comps: bool = False,
        coord_out: Literal["E", "G", "C"] = "E",
        colorcorr_table: Optional[NDArray[np.float_]] = None,
    ) -> Quantity[u.MJy / u.sr]:
        """Returns a Zodiacal Emission timestream.

        This function takes as arguments a frequency or wavelength (`freq`),
        time of observation (`obs_time`), an observer (`obs`) for which the
        position is computed using the ephemeris specified when initializing
        `Zodipy` or an observer position (`obs_pos`) which overrides `obs`.
        Furthermore, the pointing of the observer is specified either in the
        form of angles on the sky (`theta`, `phi`), or as HEALPIX pixels at
        some resolution (`pixels`, `nside`).

        Parameters
        ----------
        freq
            Frequency or wavelength at which to evaluate the Zodiacal emission.
            Must have units compatible with Hz or m.
        obs_time
            Time of observation (`astropy.time.Time` object).
        obs
            The solar system observer. A list of all support observers (for a
            given ephemeridis) is specified in `observers` attribute of the
            `zodipy.Zodipy` instance. Defaults to 'earth'.
        obs_pos
            The heliocentric ecliptic cartesian position of the observer in AU.
            Overrides the `obs` argument. Default is None.
        pixels
            A single, or a sequence of HEALPIX pixels representing points on
            the sky.
        theta, phi
            Angular coordinates (co-latitude, longitude) of a point, or a
            sequence of points, on the sphere. Units must be radians or degrees.
        nside
            HEALPIX map resolution parameter of the pixels (and optionally the
            binned output map). Must be specified if `pixels` is provided or if
            `binned` is set to True.
        lonlat
            If True, input angles (`theta`, `phi`) are assumed to be longitude
            and latitude, otherwise, they are co-latitude and longitude.
        return_comps
            If True, the emission is returned component-wise. Defaults to False.
        binned
            If True, the emission is binned into a HEALPIX map with resolution
            given by the `nside` argument. Defaults to False.
        coord_out
            Coordinate frame of the output map. Available options are: 'E', 'G',
            and 'C'. Defaults to 'E' (Ecliptic coordinates)
        colorcorr_table
            An array of shape (2, n) where the first column is temperatures
            in K, and the second column the corresponding color corrections.
            The color corrections should be for B(lambda, T) and is only applied
            to the thermal contribution of the emission.

        Returns
        -------
        emission
            Sequence of simulated Zodiacal emission in units of 'MJy/sr' for
            each pointing. If the pointing is provided in a time-ordered manner,
            then the output of this function can be interpreted as the observered
            Zodiacal Emission timestream.
        """

        if obs.lower() not in self.observers:
            raise ValueError(
                f"observer {obs!r} not supported by ephemeridis "
                f"{solar_system_ephemeris._value!r}"
            )

        if pixels is not None:
            if nside is None:
                raise ValueError(
                    "get_time_ordered_emission() got an argument for 'pixels', "
                    "but argument 'nside' is missing"
                )
            pixels = np.expand_dims(pixels, axis=0) if np.size(pixels) == 1 else pixels

        if binned and nside is None:
            raise ValueError(
                "get_time_ordered_emission() has 'binned' set to True, but "
                "argument 'nside' is missing"
            )

        if (theta is not None and phi is None) or (phi is not None and theta is None):
            raise ValueError(
                "get_time_ordered_emission() is missing an argument for either "
                "'phi' or 'theta'"
            )

        if theta is not None and phi is not None:
            phi = np.expand_dims(phi, axis=0) if phi.size == 1 else phi
            theta = np.expand_dims(theta, axis=0) if theta.size == 1 else theta
            if lonlat:
                theta = theta.to(u.deg)
                phi = phi.to(u.deg)

        if pixels is not None:
            if phi is not None or theta is not None:
                raise ValueError(
                    "get_time_ordered_emission() got an argument for both 'pixels'"
                    "and 'theta' or 'phi' but can only use one of the two"
                )
            if lonlat:
                raise ValueError(
                    "get_time_ordered_emission() has 'lonlat' set to True "
                    "but 'theta' and 'phi' is not given"
                )

        model = self._model

        earth_skycoord = get_body("Earth", time=obs_time)
        earth_skycoord = earth_skycoord.transform_to(HeliocentricMeanEcliptic)
        earth_pos = earth_skycoord.represent_as("cartesian").xyz.to(u.AU)
        if obs_pos is None:
            # IF observer is L2 we need to approximate the position
            if obs.lower() == "semb-l2":
                earth_length = np.linalg.norm(earth_pos)
                earth_unit_vec = earth_pos / earth_length
                semb_l2_length = earth_length + DISTANCE_FROM_EARTH_TO_L2
                obs_pos = earth_unit_vec * semb_l2_length
            else:
                obs_skycoord = get_body(obs, time=obs_time)
                obs_skycoord = obs_skycoord.transform_to(HeliocentricMeanEcliptic)
                obs_pos = obs_skycoord.represent_as("cartesian").xyz.to(u.AU)

        # We reshape the coordinates to broadcastable shapes.
        obs_pos = obs_pos.reshape(3, 1)
        earth_pos = earth_pos.reshape(3, 1)

        freq = freq.to("GHz", equivalencies=u.spectral())

        phase_coeffs = interp_phase_coeffs(
            freq,
            phase_coeffs=model.phase_coeffs,
            phase_coeffs_spectrum=model.phase_coeffs_spectrum,
        )
        spectral_comp_parameters = interp_comp_spectral_params(
            freq=freq,
            emissivities=model.emissivities,
            emissivity_spectrum=model.emissivity_spectrum,
            albedos=model.albedos,
            albedo_spectrum=model.albedo_spectrum,
        )
        cloud_offset = model.comps[CompLabel.CLOUD].X_0

        if binned:
            if pixels is not None:
                unique_pixels, counts = np.unique(pixels, return_counts=True)
                unit_vectors = _rotated_pix2vec(
                    pixels=unique_pixels,
                    coord_out=coord_out,
                    nside=nside,
                )

            else:
                unique_angles, counts = np.unique(
                    np.asarray([theta, phi]), return_counts=True, axis=1
                )
                unique_pixels = hp.ang2pix(nside, *unique_angles, lonlat=lonlat)
                unit_vectors = _rotated_ang2vec(
                    *unique_angles,
                    lonlat=lonlat,
                    coord_out=coord_out,
                )

            emission = np.zeros((model.ncomps, hp.nside2npix(nside)))
            for idx, (label, component) in enumerate(model.comps.items()):
                emission[idx, unique_pixels] = trapezoidal(
                    comp=component,
                    freq=freq.value,
                    line_of_sight=self._line_of_sights[label].value,
                    observer_pos=obs_pos.value,
                    earth_pos=earth_pos.value,
                    unit_vectors=unit_vectors,
                    cloud_offset=cloud_offset,
                    T_0=model.T_0.value,
                    delta=model.delta,
                    emissivity=spectral_comp_parameters[label]["emissivity"],
                    albedo=spectral_comp_parameters[label]["albedo"],
                    phase_coeffs=phase_coeffs,
                    colorcorr_table=colorcorr_table,
                )

            emission[:, unique_pixels] *= counts

        else:
            if pixels is not None:
                unique_pixels, indicies = np.unique(pixels, return_inverse=True)
                unit_vectors = _rotated_pix2vec(
                    pixels=unique_pixels,
                    coord_out=coord_out,
                    nside=nside,
                )
                emission = np.zeros((model.ncomps, len(pixels)))

            else:
                unique_angles, indicies = np.unique(
                    np.asarray([theta, phi]), return_inverse=True, axis=1
                )
                unit_vectors = _rotated_ang2vec(
                    *unique_angles,
                    lonlat=lonlat,
                    coord_out=coord_out,
                )
                emission = np.zeros((model.ncomps, len(theta)))

            for idx, (label, component) in enumerate(model.comps.items()):
                integrated_comp_emission = trapezoidal(
                    comp=component,
                    freq=freq.value,
                    line_of_sight=self._line_of_sights[label].value,
                    observer_pos=obs_pos.value,
                    earth_pos=earth_pos.value,
                    unit_vectors=unit_vectors,
                    cloud_offset=cloud_offset,
                    T_0=model.T_0.value,
                    delta=model.delta,
                    emissivity=spectral_comp_parameters[label]["emissivity"],
                    albedo=spectral_comp_parameters[label]["albedo"],
                    phase_coeffs=phase_coeffs,
                    colorcorr_table=colorcorr_table,
                )

                emission[idx] = integrated_comp_emission[indicies]

        # The output unit is W/Hz/m^2/sr which we convert to MJy/sr
        emission = (emission << (u.W / u.Hz / u.m ** 2 / u.sr)).to(u.MJy / u.sr)

        return emission if return_comps else emission.sum(axis=0)


def _rotated_pix2vec(
    pixels: Union[Sequence[int], NDArray[np.int_]], nside: int, coord_out: str
) -> NDArray[np.float_]:
    """Returns rotated unit vetors from pixels.

    Since the Interplanetary Dust Model is evaluated in Ecliptic coordinates,
    we need to rotate any unit vectors defined in another coordinate frame to
    ecliptic before evaluating the model.
    """

    unit_vectors = np.asarray(hp.pix2vec(nside, pixels))
    if coord_out != "E":
        unit_vectors = np.asarray(
            hp.rotator.Rotator(coord=[coord_out, "E"])(unit_vectors)
        )

    return unit_vectors


def _rotated_ang2vec(
    phi: Union[Quantity[u.rad], Quantity[u.deg]],
    theta: Union[Quantity[u.rad], Quantity[u.deg]],
    lonlat: bool,
    coord_out: str,
) -> NDArray[np.float_]:
    """Returns rotated unit vectors from angles.

    Since the Interplanetary Dust Model is evaluated in Ecliptic coordinates,
    we need to rotate any unit vectors defined in another coordinate frame to
    ecliptic before evaluating the model.
    """

    unit_vectors = np.asarray(hp.ang2vec(phi, theta, lonlat=lonlat)).transpose()
    if coord_out != "E":
        unit_vectors = np.asarray(
            hp.rotator.Rotator(coord=[coord_out, "E"])(unit_vectors)
        )
    return unit_vectors