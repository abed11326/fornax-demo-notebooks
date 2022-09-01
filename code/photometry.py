import sys
import os
import warnings
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS
import numpy as np

# temporarily let the notebook start without tractor as dependency
try:
    from tractor import (Tractor, PointSource, PixPos, Flux, PixelizedPSF, NullWCS,
                         NullPhotoCal, ConstantSky, Image)

    from find_nconfsources import find_nconfsources
except ImportError:
    print("tractor is missing")
    pass

from exceptions import CutoutError
from extract_cutout import extract_cutout


def setup_for_tractor(band_configs, ra, dec, stype, ks_flux_aper2, infiles, df):
    # tractor doesn't need the entire image, just a small region around the object of interest
    subimage, x1, y1, subimage_wcs, bgsubimage = make_cutouts(
        ra, dec, infiles, band_configs.cutout_width, band_configs.mosaic_pix_scale
    )

    # set up the source list by finding neighboring sources
    objsrc, nconfsrcs = find_nconfsources(
        ra, dec, stype, ks_flux_aper2, x1, y1, band_configs.cutout_width, subimage_wcs, df
    )

    # measure sky noise and mean level
    # suppress warnings about nans in the calculation
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        skymean, skymedian, skynoise = sigma_clipped_stats(
            bgsubimage, sigma=3.0)

    return subimage, objsrc, nconfsrcs, skymean, skynoise


def make_cutouts(ra, dec, infiles, cutout_width, mosaic_pix_scale):
    """Create cutouts from science and background images.

    Raise an error if a cutout cannot be extracted.
    """
    imgfile, skybgfile = infiles

    # extract science image cutout
    hdulist = fits.open(imgfile)[0]
    wcs_info = WCS(hdulist)
    subimage, nodata_param, x1, y1, subimage_wcs = extract_cutout(
        ra, dec, cutout_width, mosaic_pix_scale, hdulist, wcs_info
    )

    # if cutout extraction failed, raise an error
    if nodata_param is True:
        raise CutoutError("Cutout could not be extracted")

    # extract sky background cutout
    # if input is same as above, don't redo the cutout, just return
    if skybgfile == imgfile:
        return subimage, x1, y1, subimage_wcs, subimage

    # else continue with the extraction
    bkg_hdulist = fits.open(skybgfile)[0]
    bgsubimage, bgnodata_param, bgx1, bgy1, bgimage_wcs = extract_cutout(
        ra, dec, cutout_width, mosaic_pix_scale, bkg_hdulist, wcs_info
    )

    # if cutout extraction failed, raise an error
    if bgnodata_param is True:
        raise CutoutError("Cutout could not be extracted")

    return subimage, x1, y1, subimage_wcs, bgsubimage


def run_tractor(subimage, prf, objsrc, skymean, skynoise):
    # make the tractor image
    tim = Image(
        data=subimage,
        invvar=np.ones_like(subimage) / skynoise**2,
        psf=PixelizedPSF(prf),
        wcs=NullWCS(),
        photocal=NullPhotoCal(),
        sky=ConstantSky(skymean),
    )

    # make tractor object combining tractor image and source list
    tractor = Tractor([tim], objsrc)  # [src]

    # freeze the parameters we don't want tractor fitting
    tractor.freezeParam("images")  # now fits 2 positions and flux
    # tractor.freezeAllRecursive()#only fit for flux
    # tractor.thawPathsTo('brightness')

    # run the tractor optimization (do forced photometry)
    # Take several linearized least squares steps
    fit_fail = False
    try:
        tr = 0
        with suppress_stdout():
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", ".*divide by zero.*")
                # warnings.simplefilter('ignore')
                for tr in range(20):
                    dlnp, X, alpha, flux_var = tractor.optimize(variance=True)
                    # print('dlnp',dlnp)
                    if dlnp < 1e-3:
                        break

    # catch exceptions and bad fits
    except Exception:
        fit_fail = True
        flux_var = np.nan

    return flux_var, fit_fail


def interpret_tractor_results(flux_var, flux_conv, fit_fail, objsrc, nconfsrcs):
    # record the photometry results
    if fit_fail:
        # tractor fit failed
        # set flux and uncertainty as nan and move on
        return (np.nan, np.nan)

    elif flux_var is None:
        # fit worked, but flux variance did not get reported
        params_list = objsrc[0].getParamNames()
        bindex = params_list.index("brightness.Flux")
        flux = objsrc[0].getParams()[bindex]
        # convert to microjanskies
        microJy_flux = flux * flux_conv
        return (microJy_flux, np.nan)

    else:
        # fit and variance worked
        params_list = objsrc[0].getParamNames()
        bindex = params_list.index("brightness.Flux")
        flux = objsrc[0].getParams()[bindex]

        # determine flux uncertainty
        # which value of flux_var is for the flux variance?
        # assumes we are fitting positions and flux
        fv = ((nconfsrcs + 1) * 3) - 1
        # fv = ((nconfsrcs+1)*1) - 1  #assumes we are fitting only flux

        tractor_std = np.sqrt(flux_var[fv])

        # convert to microjanskies
        microJy_flux = flux * flux_conv
        microJy_unc = tractor_std * flux_conv
        return (microJy_flux, microJy_unc)


@contextmanager
def suppress_stdout():
    """Supress output of tractor.

    Seems to be the only way to make it be quiet and not output every step of optimization
    https://stackoverflow.com/questions/2125702/how-to-suppress-console-output-in-python
    """
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
