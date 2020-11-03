import h5py
import numpy as np
import matplotlib.pyplot as plt
import logging
import re
import time
import psana
import yaml
from scipy.optimize import curve_fit
from argparse import ArgumentParser
import json
import os
import sys
from bokeh.plotting import figure, show, output_file
from bokeh.models import Span, Legend, LegendItem, ColorBar, LinearColorMapper
from bokeh.io import output_notebook
import panel as pn
from scipy.stats import gaussian_kde

logger = logging.getLogger(__name__)

# Constants
# Location of my branch for testing
JT_LOC = '/cds/home/a/aegger/jet_tracking/jet_tracking/mpi_scripts/'
# Number of bins
BINS = 100
# Number of events to process
EVENTS = 1000
# Percent of max in histogram to reject for I0 data
LR_THRESH = 0.1
# Number of points at each end of azav to fit
LINE_FIT_POINTS = 5
# Number of bins around peak to include in integrated intensity
DELTA_BIN = 5

# For now hold equations here since, will consolidate

def get_r_masks(shape, bins=100):
    """Function to generate radial masks for pixels to include in azav"""
    center = (shape[1] / 2, shape[0] / 2)
    x, y = np.meshgrid(np.arange(shape[1]) - center[0], \
        np.arange(shape[0]) - center[1])
    r = np.sqrt(x**2 + y**2)
    max_r = np.max(r)
    min_r = np.min(r)
    bin_size = (max_r - min_r) / bins
    radii = np.arange(1, max_r, bin_size)
    masks = []
    for i in radii:
        mask = (np.greater(r, i - bin_size) & np.less(r, i + bin_size))
        masks.append(mask)

    return masks

def gaussian(x, a, mean, std, m, b):
    """
    Equation for gaussian with linear component

    Parameters
    ----------
    x : float
        X-coordinate.

    a : float
        Amplitude of Gaussian.

    mean : float
        Mean of Gaussian.

    std : float
        Standard deviation of Gaussian.

    m : float
        Slope of linear baseline.

    b : float
        Y-intercept of linear baseline.

    Returns
    -------
    y : float
        The y-coordinate for the given x-coordinate as defined by the
        parameters given for the Gaussian on a slope with offset.
    """
    return (a * np.exp(-((x - mean) / 2 / std) ** 2)) + (m * x + b)

def fit_line(ave_azav, fit_points=LINE_FIT_POINTS):
    """
    Fit the line from edges of array
    
    Parameters
    ----------
    ave_azav: ndarray
        The average azimuthal average from calibration data 1D array of floats.

    fit_points: int (Default: 5)
        Number of points at each end of average azimuthal average to fit.

    Returns
    -------
    m: float
        slope of the fit
    
    b: float
        y intercept of fit
    """
    azav_len = len(ave_azav)
    x0 = fit_points / 2
    x1 = azav_len - (fit_points / 2)
    y0 = np.mean(ave_azav[:fit_points])
    y1 = np.mean(ave_azav[azav_len - fit_points:])
    m, b = np.polyfit((x0, x1), (y0, y1), 1)

    return m, b

def peak_lr(array_data, threshold=LR_THRESH, bins=BINS):
    """Find max of normal distribution from histogram, 
    search right and left until population falls below threshold, 
    will run into problems with bimodal distribution.  This is naive,
    look at KDE

    Parameters
    ----------
    array_data: array like
        1D array data to cut on
    
    threshold: float
        percent of max population to throw out.  Upper/lower

    bins: int
        Number of bins to generate for histogram we cut on

    Returns
    -------
    hist: np.histogram
        Histogram from array_data and bins

    edges: ndarray
        array of floats for edges of histogram fit

    peak_idx: int
        peak index of histogram

    i0_low: float
        lower i0 value of cut on histogram

    i0_high: float
        upper i0 value of cut on histogram

    i0_med: float
        median i0 value
    """
    hist, edges = np.histogram(array_data, bins=bins)

    # Find peak information
    peak_val = hist.max()
    peak_idx = np.where(hist == peak_val)[0][0]
    i0_med = edges[peak_idx]

    #search right
    right = np.argmax(hist[peak_idx:] < threshold * peak_val)
    right += peak_idx
    i0_high = edges[right]

    # search left
    left_array = hist[:peak_idx]
    left = peak_idx - np.argmax(left_array[::-1] < threshold * peak_val)
    i0_low = edges[left]

    return hist, peak_idx, i0_low, i0_high, i0_med

def calc_azav_peak(ave_azav):
    """
    Get the peak from the gaussian fit with linear offset, if this
    fails, just use the max value.  This could also use KDE.

    Parameters
    ----------
    ave_azav: ndarray
        radially binned average azimuthal intensity from used curves

    Returns
    -------
    peak: int
        index of the bin with the peak intensity
    """
    azav_len = len(ave_azav)
    # Fit the gaussian with line offset, fails for skewed gaussian
    m, b = fit_line(ave_azav)
    x = np.arange(azav_len)
    mean = np.sum(x * ave_azav) / np.sum(ave_azav)
    std = np.sqrt(np.sum((x - mean) ** 2 / azav_len))

    # Try to get the peak from fit
    try:
        # Guass/w line args
        p0 = [max(ave_azav), mean, std, m, b]
        popt, _ = curve_fit(gaussian, x, ave_azav, p0=p0)
        peak = int(round(popt[1]))
    except Exception as e:
        logger.info('Failed to fit Gaussian, using peak')
        peak = np.argmax(ave_azav)

    return peak

def get_integrated_intensity(ave_azav, peak_bin, delta_bin=DELTA_BIN):
    """
    Get the average integrated intensity.  Sum the bin values from
    the peak bin to delta bin on each side.

    Parameters
    ----------
    ave_azav: ndarray
        radially binned average azimuthal intensity from used curves

    peak_bin: int
        peak radial bin

    delta_bin: int
        number of bins on each side of peak to include in sum

    Returns
    -------
    integrated_intensity: float
        the calculated integrated intensity
    """
    low = peak_bin - delta_bin
    high = peak_bin + delta_bin
    integrated_intensity = ave_azav[low: high].sum(axis=0)

    return integrated_intensity

def fit_limits(i0_data, peak_vals, i_low, i_high, bins=100):
    """
    Get the line fit and standard deviation of the plot of i0 
    vs int_intensities.  This will give information about
    distribution of integrated intensities for given i0 values
    
    Parameters
    ----------
    i0_data: ndarray
        Values of incoming intensities used in calibration

    peak_vals: ndarray
        Values of the integrated intensities corrspeconding to i0 values

    """
    m, b = np.polyfit(i0_data, peak_vals, 1)
    x = np.linspace(i_low, i_high, bins)
    sigma = np.std(peak_vals)
    y = x * m + b

    return x, y, m, b, sigma

          ###### Bokeh Figures #######

def peak_fig(signal, hist, edges, peak_idx, left, right):
    """General histogram plotter with peak location and 
    left/right limits plotted"""
    low = round(edges[left], 2)
    hi = round(edges[right], 2)
    fig = figure(
        title='Used Intensity Distribution for {0}. \
            Low/Hi: {1}/{2}'.format(signal, low, hi),
        x_axis_label='Intensity Values',
        y_axis_label='Counts'
    )
    fig.quad(top=hist, bottom=0, left=edges[:-1], right=edges[1:])
    left_line = Span(location=edges[left], dimension='height', \
        line_color='black')
    right_line = Span(location=edges[right], dimension='height', \
        line_color='black')
    peak_line = Span(location=edges[peak_idx], dimension='height', \
        line_color='red')
    fig.renderers.extend([left_line, right_line, peak_line])

    return fig

def azav_fig(ave_azav, peak, intensity, delta_bin):
    """Generate the azav fig for html file"""
    x_vals = np.arange(len(ave_azav))
    fig = figure(
        title='Average Azimuthal Binned Array: Center - {0}, min/max - {1}/{2}, intensity - {3}'.format(peak, peak-delta_bin, peak+delta_bin, round(intensity, 2)),
        x_axis_label='Bins',
        y_axis_label='Intensity',
    )

    peak_line = Span(location=peak, dimension='height', line_color='green', line_width=2)
    lower_line = Span(location=peak-delta_bin, dimension='height', line_color='black')
    upper_line = Span(location=peak+delta_bin, dimension='height', line_color='black')
    ave_azav_curve = fig.scatter(x_vals, ave_azav)
    fig.renderers.extend([peak_line, lower_line, upper_line])

    azav_legend = Legend(items=[
        LegendItem(label='Azimuthal Average', renderers=[ave_azav_curve])
    ])
    fig.add_layout(azav_legend)

    return fig

def intensity_hist(intensity_hist, edges):
    fig = figure(
            title='Intensity Histogram'
    )
    fig.quad(top=intensity_hist, bottom=0, left=edges[:-1], right=edges[1:])

    return fig

def intensity_vs_peak_fig(intensity, peak_vals, x, y, slope, intercept, sigma):
    """Simple plot of intensity vs peak value"""
    fig = figure(
        title='Peak value vs Intensity. Slope = {0}, Intercept = {1}'.format(round(slope, 2), round(intercept, 2)),
        x_axis_label='Intensity Monitor Value',
        y_axis_label='Peak Values'
    )
    fig.x_range.range_padding = fig.y_range.range_padding = 0
    h, y_edge, x_edge = np.histogram2d(peak_vals, intensity, bins=100)
    fig.image(image=[h], x=x_edge[0], y=y_edge[0], dh=y_edge[-1]-y_edge[0], dw=x_edge[-1]-x_edge[0], palette="Spectral11")
    color_mapper = LinearColorMapper(palette="Spectral11", low=h.min(), high=h.max())
    color_bar = ColorBar(color_mapper=color_mapper, location=(0,0))
    fig.xaxis.bounds = [i0_low, i0_high]
    fig.add_layout(color_bar, 'right')
    fig.line(x, y, color='red')
    fig.line(x, y - 1 * sigma, color='orange')
    fig.line(x, y + 1 * sigma, color='orange')
    return fig
    # 2d histogram with limits plotted
    # gspec[10:12, 0:3] = intensity_vs_peak_fig(i0_data_use, azav_intensity_vals, x, y, m, b, sigma)

    # results_dir = args.results_dir
    # if not results_dir:
    #     results_dir = f'{results_path}/results.html'

    # if not os.path.exists(results_dir):
    #     logger.debug('results directory does not exist, creating')
    #     os.makedirs(results_dir)

    # logger.debug(f'Saving html file to {results_dir}')
    # gspec.save(f'{results_path}/results.html')

    # results = {
    #     'i0_low': i0_low,
    #     'i0_high': i0_high,
    #     'peak_azav_bin': peak_azav_bin,
    #     'left_azav_bin': left_azav_bin,
    #     'right_azav_bin': right_azav_bin,
    #     'slope_fit': m,
    #     'intercept': b,
    #     'sigma': sigma
    # }
    # cal_dir = args.cal_dir
    # if not cal_dir:
    #     cal_dir = f'{cal_path}/Run_{run}'

    # write_html(cal_dir, results, run)

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--exp', type=str, default=(os.environ['EXPERIMENT']))
    parser.add_argument('--run', type=str, default=(os.environ['RUN_NUM']))
    parser.add_argument('--cfg', type=str, \
        default=(''.join([JT_LOC, 'mpi_scripts/mpi_configs/default_config.yml'])))
    args = parser.parse_args()

    with open(''.join(['mpi_configs/', args.cfg_file])) as f:
        yml_dict = yaml.load(f, Loader=yaml.FullLoader)
        api_port = yml_dict['api_msg']['port']
        det_map = yml_dict['det_map']
        ipm_name = yml_dict['ipm']['name']
        ipm_det = yml_dict['ipm']['det']
        hutch = yml_dict['hutch']

    # Setup data source
    exp_run = ''.join(['exp=', args.exp, ':run=', args.run])
    exp_dir = ''.join(['/reg/d/psdm/', hutch, '/', args.exp, '/xtc/'])
    dsname = ''.join([exp_run, ':smd:', 'dir=', exp_dir])
    ds = psana.DataSource(dsname)

    # Get the detectors from the config
    detector = psana.Detector(det_map['name'])
    ipm = psana.Detector(ipm_name)
    masks = get_r_masks((2203, 2299), BINS)
    i0_data = []
    azav_data=[]

    # Find I0 distribution and filter out unused values
    i0_data = np.array(i0_data)
    i0_hist, i0_peak_idx, i0_low, i0_high, i0_med = peak_lr(i0_data)
    i0_idxs = np.where((i0_data > i0_low) & (i0_data < i0_high))
    i0_data_use = i0_data[i0_idxs]
    p = peak_fig('gdet', i0_hist, i0_peak_idx, i0_low, i0_high)

    # Get the azav value we'll use
    azav_use = [azav_data[idx] for idx in i0_idxs[0]]
    ave_azav = np.array((np.sum(azav_use, axis=0)) / len(azav_use))
    # Find the peak bin from average azav values
    peak = calc_azav_peak(ave_azav)

    # Get the integrated intensity and generate fig
    integrated_intensity = get_integrated_intensity(ave_azav, peak, DELTA_BIN)
    p1 = azav_fig(ave_azav, peak, integrated_intensity, DELTA_BIN)


    # Go back through indices and find peak values for all the intensities
    low = peak - DELTA_BIN
    high = peak + DELTA_BIN
    peak_vals = [azav[low:high].sum(axis=0) for azav in azav_use]
    # Now fit I0 vs diffraction intensities
    x, y, slope, intercept, sigma = fit_limits(i0_data_use, peak_vals, i0_low, i0_high)
    p2 = intensity_vs_peak_fig(i0_data_use, peak_vals, x, y, slope, intercept, sigma)