#!/usr/bin/env python
"""
aroma.py: filter fmri datasets based on ICA analysis.
"""
from __future__ import division, print_function

import sys
import os
from os.path import join, isfile, isdir, exists, dirname
import shutil
from glob import glob
from tempfile import mkdtemp, mkstemp
from subprocess import call, check_output, Popen, PIPE

import logging
import argparse
import random

import numpy as np

# FSL commands and environment
FSLBINDIR = join(os.environ.get("FSLDIR", '/usr/share/fsl5.0'), 'bin')
FSLTEMPLATEDIR = join(os.environ.get("FSLDIR", '/usr/share/fsl5.0'), 'data', 'standard')

# MNI152 T1 2mm template file
FSLMNI52TEMPLATE = join(FSLTEMPLATEDIR, 'MNI152_T1_2mm_brain.nii.gz')

FSLINFO    = join(FSLBINDIR, 'fslinfo')
MELODIC    = join(FSLBINDIR, 'melodic')
FSLROI     = join(FSLBINDIR, 'fslroi')
FSLMERGE   = join(FSLBINDIR, 'fslmerge')
FSLMATHS   = join(FSLBINDIR, 'fslmaths')
FLIRT      = join(FSLBINDIR, 'flirt')
APPLYWARP  = join(FSLBINDIR, 'applywarp')
FSLSTATS   = join(FSLBINDIR, 'fslstats')
FSLREGFILT = join(FSLBINDIR, 'fsl_regfilt')
BET        = join(FSLBINDIR, 'bet')

AROMADIR = os.environ.get("AROMADIR", '/usr/local/share/ica-aroma')

def is_writable_file(path):
    exists_and_writable = path and isfile(path) and os.access(path, os.W_OK)
    parent = path and (os.path.dirname(path) or os.getcwd())
    creatable = parent and os.access(parent, os.W_OK)
    return exists_and_writable or creatable


def is_writable_directory(path):
    return path and isdir(path) and os.access(path, os.W_OK)


def nifti_info(filename, tag):
    """Extract value of tag from nifti header of file"""
    info = check_output([FSLINFO, filename], universal_newlines=True)
    fields = [line for line in info.split('\n') if line.startswith(tag)][0].split()
    return fields[-1]


def nifti_dims(filename):
    """Matrix dimensions of image in nifti file"""
    return tuple([int(float(nifti_info(filename, 'dim%d' % (i+1)))) for i in range(4)])


def nifti_pixdims(filename):
    """Pixel dimensions of image in nifti file"""
    return tuple([float(nifti_info(filename, 'pixdim%d' % (i+1))) for i in range(4)])


def zsums(filename, mask=None):
    """Sum of Z-values within the total Z-map or within a subset defined by a mask.

    Calculated via the mean and number of non-zero voxels.

    Parameters
    ----------
    filename: str
        zmap nifti file
    mask: Optional(str)
        mask file

    Returns
    -------
    numpy array
        sums of pixels across the whole images or just within the mask
    """
    assert isfile(filename)

    _, tmpfile = mkstemp(prefix='zsums', suffix='.nii.gz')

    # Change to absolute Z-values
    call([FSLMATHS, filename, '-abs', tmpfile])    
    
    preamble = [FSLSTATS, '-t', tmpfile]
    if mask is not None:
        preamble += ['-k', mask]
    counts_cmd = preamble + ['-V']
    means_cmd  = preamble + ['-M']

    p = Popen(counts_cmd, stdout=PIPE)
    counts = np.loadtxt(p.stdout)[:, 0]
    p = Popen(means_cmd, stdout=PIPE)
    means = np.loadtxt(p.stdout)

    os.unlink(tmpfile)
    return means * counts


def cross_correlation(a, b):
    """Cross Correlations between columns of two matrices"""
    assert a.ndim == b.ndim == 2
    _, ncols_a = a.shape
    # nb variables in columns rather than rows hence transpose
    # extract just the cross terms between cols in a and cols in b
    return np.corrcoef(a.T, b.T)[:ncols_a, ncols_a:]


def is_valid_melodic_dir(dirname):
    """Check for all the files needed in melodic directory"""
    return (
        dirname and
        isdir(dirname) and
        isfile(join(dirname, 'melodic_IC.nii.gz')) and
        isfile(join(dirname, 'melodic_mix')) and
        isfile(join(dirname, 'melodic_FTmix')) and
        isdir(join(dirname, 'stats'))
    )


def run_ica(infile, outfile, maskfile, t_r, ndims_ica=0, melodic_indir=None, seed=None):
    """Runs MELODIC and merges the mixture modelled thresholded ICs into a single 4D nifti file.

    Parameters
    ----------
    infile:  str
        fMRI data file (nii.gz) on which MELODIC should be run
    outdir:  str
        Output directory
    maskfile: str
        Mask file to be applied during MELODIC
    t_r: float
        Repetition Time (in seconds) of the fMRI data
    ndims_ica: int
        Dimensionality of ICA
    melodic_indir: str
        MELODIC directory in case it has been run before
    seed: Optional(unsigned int)
        Seed for RNG in melodic
        
    Returns
    -------
    tuple of numpy arrays
        mix, ftmix

    Output
    ------
    Merged file containing the mixture modelling thresholded Z-stat maps
    """
    assert isfile(maskfile)
    assert 0.5 < t_r < 10
    assert 0 <= ndims_ica < 100

    working_dir = mkdtemp(prefix='run_ica')

    if is_valid_melodic_dir(melodic_indir):
        for f in ['melodic_IC.nii.gz', 'melodic_mix', 'melodic_FTmix', 'stats']:
            shutil.copytree(join(melodic_indir, f), join(working_dir, f))
    else:
        cmdline = [MELODIC, '--in=%s' % infile, '--outdir=%s' % working_dir,
            '--mask=%s' % maskfile, '--dim=%d' % ndims_ica,
            '--Ostats', '--nobet', '--mmthresh=0.5', '--report', '--tr=%f' % t_r
        ]
        if seed is not None:
            cmdline.append('--seed=%u' % seed)
        call(cmdline)

    assert is_valid_melodic_dir(working_dir)

    melodic_ics_file   = join(working_dir, 'melodic_IC.nii.gz')
    melodic_ftmix_file = join(working_dir, 'melodic_FTmix')
    melodic_mix_file   = join(working_dir, 'melodic_mix')

    # Normally, there will be only one spatial map per file but if the mixture modelling did not converge
    # there will be two, the latter being the results from a simple null hypothesis test and the first one empty.
    # To handle this we'll get the last map from each file.
    # NB Files created by MELODIC are labelled with integers, base 1, no zero padding ... 
    ncomponents = nifti_dims(melodic_ics_file)[3]
    zfiles_in  = [join(working_dir, 'stats', 'thresh_zstat%d.nii.gz' % i) for i in range(1, ncomponents+1)]
    zfiles_out = [join(working_dir, 'stats', 'thresh_zstat_fixed%d.nii.gz' % i) for i in range(1, ncomponents+1)]
    for zfile_in, zfile_out in zip(zfiles_in, zfiles_out):
        nmaps = nifti_dims(zfile_in)[3] # will be 1 or 2
        #             input,      output, first frame (base 0), number of frames 
        call([FSLROI, zfile_in, zfile_out, '%d' % (nmaps-1), '1'])       

    # Merge all mixture modelled Z-maps within the output directory (NB: -t => concatenate in time)
    melodic_thr_file = join(working_dir, 'melodic_IC_thr.nii.gz')
    call([FSLMERGE, '-t', melodic_thr_file] + zfiles_out)

    # Apply the mask to the merged file (in case pre-run melodic was run with a different mask)
    call([FSLMATHS, melodic_thr_file, '-mas', maskfile, melodic_thr_file])

    # Outputs
    shutil.copyfile(melodic_thr_file, outfile)
    mix = np.loadtxt(melodic_mix_file)
    ftmix = np.loadtxt(melodic_ftmix_file)

    shutil.rmtree(working_dir)
    return mix, ftmix


def register_to_mni(infile, outfile, template=FSLMNI52TEMPLATE, affmat=None, warp=None):
    """Registers an image (or a time-series of images) to MNI152 T1 2mm.

    If no affmat is specified, it only warps (i.e. it assumes that the data has been registered to the
    structural scan associated with the warp-file already). If no warp is specified either, it only
    resamples the data to 2mm isotropic if needed (i.e. it assumes that the data has been registered
    to a MNI152 template). In case only an affmat file is specified, it assumes that the data has to be
    linearly registered to MNI152 (i.e. the user has a reason not to use non-linear registration on the data).
    TODO: RHD this is nasty overloading of meaning of args
    Parameters
    ----------
    infile: str
        Input file (nii.gz) which is to be registered to MNI152 T1 2mm
    outfile: str
        Output file registered to MNI152 T1 2mm (.nii.gz)
    template: Optional(str)
        MNI52 template file
    affmat: str
        Mat file describing the linear registration to structural space (if image still in native space)
    warp: str
        Warp file describing the non-linear registration to MNI152 space (if image not yet in MNI space)

    Returns
    -------
    None

    Output 
    ------
    File containing the mixture modelling thresholded Z-stat maps registered to 2mm MNI152 template
    """
    assert isfile(infile)
    assert is_writable_file(outfile)

    if affmat is None and warp is None:
        # No affmat- or warp-file specified, assume already in MNI152 space
        if np.allclose(nifti_pixdims(infile)[:3], [2.0, 2.0, 2.0]):
            shutil.copyfile(src=infile, dst=outfile)
        else:
            # Resample to 2mm if need be
            call([
                FLIRT, '-ref', template, '-in', infile, '-out', outfile,
                '-applyisoxfm', '2', '-interp', 'trilinear'
            ])
    elif warp is not None and affmat is None:
        # Only a warp-file, assume already registered to structural, apply warp only
        call([
            APPLYWARP, '--ref=%s' % template, '--in=%s' % infile, '--out=%s' % outfile,
            '--warp=%s' % warp, '--interp=trilinear'
        ])
    elif affmat is not None and warp is None:
        # Only a affmat-file, perform affine registration to MNI
        call([
            FLIRT, '-ref', template, '-in', infile, '-out', outfile,
            '-applyxfm', '-init', affmat, '-interp', 'trilinear'
        ])
    else:
        # Both an affmat and a warp file specified, apply both
        call([
            APPLYWARP, '--ref=%s' % template, '--in=%s' % infile, '--out=%s' % outfile,
            '--warp=%s' % warp, '--premat=%s' % affmat, '--interp=trilinear'
        ])


def feature_time_series(mix, rparams, seed=None):
    """Maximum realignment parameters correlation feature scores.

    Determines the maximum robust correlation of each component time-series with
    a model of 72 realigment parameters.

    Parameters
    ----------
    mix: rank 2 numpy array
        Melodic_mix array
    rparams: rank 2 nump array
        Realignment parameters (n rows of 6 parameters)
    seed: Optional(int)
        Random number generator seed for python random module
    Returns
    -------
    rank 1 numpy.array
        Maximum RP correlation feature scores for the components of the melodic_mix file
    """
    assert mix.ndim == rparams.ndim == 2

    _, nparams = rparams.shape

    if seed is not None:
        random.seed(seed)

    # RP model including the RPs, their derivatives, and time shifted versions of each
    rp_derivs = np.vstack((
        np.zeros(nparams),
        np.diff(rparams, axis=0)
    ))
    rp12 = np.hstack((rparams, rp_derivs))
    rp12_1fw = np.vstack((
        np.zeros(2*nparams),
        rp12[:-1]
    ))
    rp12_1bw = np.vstack((
        rp12[1:],
        np.zeros(2*nparams)
    ))
    rp_model = np.hstack((rp12, rp12_1fw, rp12_1bw))

    # Determine the maximum correlation between RPs and IC time-series
    nsplits = 1000
    nmixrows, nmixcols = mix.shape
    nrows_to_choose = int(round(0.9 * nmixrows))

    max_correls = np.empty((nsplits, nmixcols))
    for i in range(nsplits):
        # Select a random subset of 90% of the dataset rows (*without* replacement)
        chosen_rows = random.sample(population=range(nmixrows), k=nrows_to_choose)

        # Combined correlations between RP and IC time-series, squared and non squared
        correl_nonsquared = cross_correlation(mix[chosen_rows], rp_model[chosen_rows])
        correl_squared = cross_correlation(mix[chosen_rows]**2, rp_model[chosen_rows]**2)
        correl_both = np.hstack((correl_squared, correl_nonsquared))

        # Maximum absolute temporal correlation for every IC
        max_correls[i] = np.abs(correl_both).max(axis=1)

    # Feature score is the mean of the maximum correlation over all the random splits
    return max_correls.mean(axis=0)


def feature_frequency(ftmix, t_r):
    """High-frequency content feature scores.

    It determines the frequency, as fraction of the Nyquist frequency, at which the higher and lower
    frequencies explain half of the total power between 0.01Hz and Nyquist.

    Parameters
    ----------
    ftmix: rank 2 numpy array
        melodic ft mix array
    t_r: float
        Repetition time (in seconds) of the fMRI data

    Returns
    -------
    rank 1 numpy.array
        HFC ('High-frequency content') feature scores for the components of the melodic_FTmix file
    """
    assert ftmix.ndim == 2
    assert 0.5 < t_r < 10

    sample_frequency = 1 / t_r
    nyquist = sample_frequency / 2

    # Determine which frequencies are associated with every row in the melodic_FTmix file
    # (assuming the rows range from 0Hz to Nyquist)
    # TODO: RHD: Off by one? is the first row 0Hz or nyquist/n and the last (n-1)/n * nyquist or nyquist?
    # TODO: - How many rows?
    frequencies = nyquist * (np.arange(ftmix.shape[0]) + 1) / ftmix.shape[0]

    # Include only frequencies above 0.01 Hz
    ftmix = ftmix[frequencies > 0.01, :]
    frequencies = frequencies[frequencies > 0.01]

    # Set frequency range to [0, 1]
    normalised_frequencies = (frequencies - 0.01) / (nyquist - 0.01)
    
    # For every IC; get the cumulative sum as a fraction of the total sum
    fcumsum_fraction = np.cumsum(ftmix, axis=0) / np.sum(ftmix, axis=0)

    # Determine the index of the frequency with the fractional cumulative sum closest to 0.5
    # (RHD: that's a weird way to get a zero crossing)
    index_cutoff = np.argmin((fcumsum_fraction - 0.5)**2, axis=0)

    # Now get the fractions associated with those indices index, these are the final feature scores
    hfc = normalised_frequencies[index_cutoff]

    # Return 'High-frequency content' feature score
    return hfc


def feature_spatial(melodic_ic_file, aroma_dir=None):
    """Spatial feature scores.

    For each IC determine the fraction of the mixture modelled thresholded Z-maps respectively located within
    the CSF or at the brain edges, using predefined standardized masks.

    Parameters
    ----------
    melodic_ic_file: str
        nii.gz file containing mixture-modelled thresholded (p>0.5) Z-maps, registered to the MNI152 2mm template
    aroma_dir:  str
        ICA-AROMA directory, containing the mask-files (mask_edge.nii.gz, mask_csf.nii.gz & mask_out.nii.gz)

    Returns
    -------
    tuple (rank 1 array like, rank 1 array like)
        Edge and CSF fraction feature scores for the components of the melodic_ics_file file
    """
    assert isfile(melodic_ic_file)
    if aroma_dir is None:
        aroma_dir = AROMADIR
    assert isdir(aroma_dir)

    edge_mask = join(aroma_dir, 'mask_edge.nii.gz')
    csf_mask  = join(aroma_dir, 'mask_csf.nii.gz')
    out_mask  = join(aroma_dir, 'mask_out.nii.gz')

    total_sum = zsums(melodic_ic_file)
    csf_sum = zsums(melodic_ic_file, mask=csf_mask)
    edge_sum = zsums(melodic_ic_file, mask=edge_mask)
    outside_sum = zsums(melodic_ic_file, mask=out_mask)

    edge_fraction = np.where(total_sum > csf_sum, (outside_sum + edge_sum) / (total_sum - csf_sum), 0)
    csf_fraction = np.where(total_sum > csf_sum, csf_sum / total_sum, 0)
    
    return edge_fraction, csf_fraction


def classification(max_rp_correl, edge_fraction, hfc, csf_fraction):
    """Classify a set of components into motion and non-motion components.

    Classification is based on four features:
     - maximum RP correlation
     - high-frequency content,
     - edge-fraction
     - CSF-fraction

    Parameters
    ----------
    max_rp_correl: rank 1 array like
        Maximum RP Correlation feature scores of the components
    edge_fraction: rank 1 array like
        Edge Fraction feature scores of the components
    hfc: rank1 array like
        High-Frequency Content feature scores of the components
    csf_fraction:  ranke 1 array like
        CSF fraction feature scores of the components

    Return
    ------
    rank 1 numpy array
        Indices of the components identified as motion components
    """
    assert len(max_rp_correl) == len(edge_fraction) == len(hfc) == len(csf_fraction)

    # Criteria for classification (thresholds and hyperplane-parameters)
    csf_threshold = 0.10
    hfc_threshold = 0.35
    hyperplane = np.array([-19.9751070082159, 9.95127547670627, 24.8333160239175])

    # Project edge and max_rp_correl feature scores to new 1D space
    projection = hyperplane[0] + np.vstack([max_rp_correl, edge_fraction]).T.dot(hyperplane[1:])

    # NB np.where() with single arg returns list of indices satisfying condition
    return np.where((projection > 0) | (csf_fraction > csf_threshold) | (hfc > hfc_threshold))[0]


def save_classification(outdir, max_rp_correl, edge_fraction, hfc, csf_fraction, motion_ic_indices):
    """Save classification results in text files.

    Parameters
    ----------
    outdir: str
        Output directory
    max_rp_correl: rank 1 array like
        Maximum RP Correlation feature scores of the components
    edge_fraction: rank 1 array like
        Edge Fraction feature scores of the components
    hfc: rank1 array like
        High-frequency content' feature scores of the components
    csf_fraction:  rank 1 array like
        CSF fraction feature scores of the components
    motion_ic_indices:  rank 1 array like
        list of indices of components classified as motion

    Return
    ------
    None

    Output (within the requested output directory)
    ------
    text file containing the original feature scores (feature_scores.txt)
    text file containing the indices of the identified components (classified_motion_ICs.txt)
    text file containing summary of classification (classification_overview.txt)
    """
    assert is_writable_directory(outdir)
    assert max(motion_ic_indices) < len(max_rp_correl) == len(edge_fraction) == len(hfc) == len(csf_fraction)

    # Feature scores
    np.savetxt(join(outdir, 'feature_scores.txt'),
               np.vstack((max_rp_correl, edge_fraction, hfc, csf_fraction)).T)

    # Indices of motion-classified ICs
    with open(join(outdir, 'classified_motion_ICs.txt'), 'w') as file_:
        if len(motion_ic_indices) > 0:
            print(','.join(['%.0f' % (idx+1) for idx in motion_ic_indices]), file=file_)

    # Summary overview of the classification, RHD: layout adjusted to be valid tsv
    is_motion = np.zeros_like(csf_fraction, dtype=bool)
    is_motion[motion_ic_indices] = True
    with open(join(outdir, 'classification_overview.txt'), 'w') as file_:
        print(
            'IC', 'Motion/noise', 'maximum RP correlation', 'Edge-fraction', '', 'High-frequency content', 'CSF-fraction',
            sep='\t', file=file_
        )
        for i in range(len(csf_fraction)):
            print('%d\t%s\t\t%.2f\t\t\t%.2f\t\t\t%.2f\t\t\t%.2f' %
                    (i+1, is_motion[i], max_rp_correl[i],
                     edge_fraction[i], hfc[i], csf_fraction[i]),
                  file=file_
            )


def denoising(infile, outfile, mix, denoise_indices, aggressive=False):
    """Apply reg_filt ica denoising using the specified components

    Parameters
    ----------
    infile: str
        Input data file (nii.gz) to be denoised
    outfile: str
        Output file
    mix: rank 2 numpy array
        Melodic mix matrix
    denoise_indices:  rank 1 numpy array like
        Indices of the components that should be regressed out
    aggressive: bool
        Whether to do aggressive denoising
    Returns
    -------
    None

    Output (within the requested output directory)
    ------
    A nii.gz file of the denoised fMRI data (denoised_func_data_<denoise_type>.nii.gz) in outdir
    """
    assert isfile(infile)
    assert is_writable_file(outfile)
    assert mix.ndim == 2

    fd, melmix_file = mkstemp(prefix='denoising', suffix='.txt')
    np.savetxt(melmix_file, mix)

    if len(denoise_indices) > 0:
        index_list = ','.join(['%d' % (i+1) for i in denoise_indices])
        regfilt_args = [
            '--in=' + infile, '--design=' + melmix_file,
            '--filter=%s' % index_list,
            '--out=' + outfile
        ]
        if aggressive:
            regfilt_args.append('-a')
        call([FSLREGFILT] + regfilt_args)
    else:
        logging.warning(
            "denoising: None of the components was classified as motion, so no denoising was applied" +
            "(a copy of the input file has been made)."
        )
        shutil.copyfile(infile, outfile)

    os.unlink(melmix_file)
    os.close(fd)


def parse_cmdline(args):
    """Parse command line arguments.
    """
    def valid_infile(arg):
        if args is None or os.path.isfile(arg):
            return arg
        else:
            raise argparse.ArgumentTypeError("{0} does not exist".format(arg))
    def valid_indir(arg):
        if args is None or os.path.isdir(arg):
            return arg
        else:
            raise argparse.ArgumentTypeError("{0} does not exist".format(arg))

    parser = argparse.ArgumentParser(
        description=(
            'ICA-AROMA v0.3beta ("ICA-based Automatic Removal Of Motion Artefacts" on fMRI data).' +
            ' See the companion manual for further information.'))

    # Required arguments
    requiredargs = parser.add_argument_group('Required arguments')
    requiredargs.add_argument('-o', '-out', dest="outdir", required=True, help='Output directory name')

    # Required arguments in non-Feat mode
    nonfeatargs = parser.add_argument_group('Required arguments - generic mode')
    nonfeatargs.add_argument('-i', '--in', dest="infile", type=valid_infile, help='Input file name of fMRI data (.nii.gz)')
    nonfeatargs.add_argument(
        '-p', '--motionparams', dest="mc", type=valid_infile,
        help='mc motion correction file eg prefiltered_func_data_mcf.par')
    nonfeatargs.add_argument(
        '-a', '--affmat', dest="affmat", type=valid_infile,
        help=(
            'Mat file of the affine registration (eg FLIRT) of the functional data to structural space.' +
            ' (.mat file eg subj.feat/reg/example_func2highres.mat)'))
    nonfeatargs.add_argument(
        '-w', '--warp', dest="warp", type=valid_infile,
        help=(
            'Warp file of the non-linear registration (eg FNIRT) of the structural data to MNI152 space .' +
            ' (.nii.gz file eg subj.feat/reg/highres2standard_warp.nii.gz)'))
    nonfeatargs.add_argument(
        '-m', '--mask', dest="mask", type=valid_infile,
        help='Mask file for MELODIC (denoising will be performed on the original/non-masked input data)')

    # Required options in Feat mode
    featargs = parser.add_argument_group('Required arguments - FEAT mode')
    featargs.add_argument(
        '-f', '--feat', dest="featdir", type=valid_indir,
        help='Existing Feat folder (Feat should have been run without temporal filtering and including' +
             'registration to MNI152)')

    # Optional options
    optionalargs = parser.add_argument_group('Optional arguments')
    optionalargs.add_argument('--tr', dest="TR", help='TR in seconds', type=float)
    optionalargs.add_argument(
        '-t', '--denoisetype', dest="denoise_type", default="nonaggr",
        choices=['no', 'nonaggr', 'aggr', 'both'],
        help=(
            "Denoising strategy: 'no': classification only; 'nonaggr':" +
            " non-aggresssive; 'aggr': aggressive; 'both': both (seperately)"))
    optionalargs.add_argument(
        '-M', '--melodicdir', dest="melodic_dir", default=None, type=valid_indir,
        help='MELODIC directory name if MELODIC has been run previously.')
    optionalargs.add_argument(
        '-D', '--dimreduction', dest="dim", default=0, type=int,
        help='Dimensionality reduction into #num dimensions when running MELODIC (default: automatic estimation)')
    optionalargs.add_argument(
        '-s', '--seed', dest="seed", default=None, type=int, help='Random number seed')
    optionalargs.add_argument(
        '-L', '--log', dest="loglevel", default='INFO', help='Logging Level')

    return parser.parse_args(args)


def feat_args(args):
    """Check feat directory and return file and directory names to use.
    """
    featdir = args.featdir
    if not isdir(featdir):
        logging.critical('The specified Feat directory (%s) does not exist. Exiting ...', featdir)
        raise ValueError('Feat directory %s does not exist or is not a directory' % featdir)

    cancelled = False

    # The names of the input files that should be already present in the Feat directory
    infile = join(featdir, 'filtered_func_data.nii.gz')
    mc = join(featdir, 'mc', 'prefiltered_func_data_mcf.par')
    affmat = join(featdir, 'reg', 'example_func2highres.mat')
    warp = join(featdir, 'reg', 'highres2standard_warp.nii.gz')

    # Check whether these files actually exist
    if not isfile(infile):
        logging.error('Missing filtered_func_data.nii.gz in Feat directory.')
        cancelled = True
    if not isfile(mc):
        logging.error('Missing mc/prefiltered_func_data_mcf.mat in Feat directory.')
        cancelled = True
    if not isfile(affmat):
        logging.error('Missing reg/example_func2highres.mat in Feat directory.')
        cancelled = True
    if not isfile(warp):
        logging.error('Missing reg/highres2standard_warp.nii.gz in Feat directory.')
        cancelled = True
    if cancelled:
        raise ValueError('Feat directory %s has missing files' % featdir)

    melodic_dir = join(featdir, 'filtered_func_data.ica')
    melodic_dir = melodic_dir if isdir(melodic_dir) else args.melodic_dir

    return infile, mc, affmat, warp, melodic_dir


def nonfeat_args(args):
    """Check explicitly passed file names.
    """
    infile = args.infile
    mc = args.mc
    affmat = args.affmat
    warp = args.warp
    melodic_dir = args.melodic_dir

    # Check whether the files exist
    if infile is None:
        logging.warning('No input file specified.')
    elif not isfile(infile):
        logging.error('The specified input file (%s) does not exist.', infile)
        raise ValueError('Missing input file')
    if mc is None:
        logging.warning('No mc file specified.')
    elif not isfile(mc):
        logging.error('The specified mc file (%s) does does not exist.', mc)
        raise ValueError('Missing Motion Parameter file')
    if affmat is not None:
        if not isfile(affmat):
            logging.error('The specified affmat file (%s) does not exist.', affmat)
            raise ValueError('Missing Affine matrix file')
    if warp is not None:
        if not isfile(warp):
            logging.error('The specified warp file (%s) does not exist.', warp)
            raise ValueError('Missing Warp file')

    return infile, mc, affmat, warp, melodic_dir


def common_args(args):
    """Extract and check common arguments.
    """
    outdir = args.outdir
    dim = args.dim
    denoise_type = args.denoise_type
    mask = args.mask
    seed = args.seed
    # Check if the mask exists, when specified.
    if mask is not None:
        if not isfile(mask):
            logging.error('The specified mask %s does not exist.', mask)
            raise ValueError('Missing Mask file')
    return outdir, dim, denoise_type, mask, seed


def create_mask(infile, outfile, featdir=None):
    """Create a mask.
    """
    assert isfile(infile)
    assert is_writable_file(outfile)

    if featdir is None:
        # RHD: just binarize stddev of input file?
        call([FSLMATHS, infile, '-Tstd', '-bin', outfile])
        return

    # Try and use example_func in feat dir to create a mask
    example_func = join(featdir, 'example_func.nii.gz')
    if isfile(example_func):
        temp_dir = mkdtemp(prefix='create_mask')
        call([BET, example_func, join(temp_dir, 'bet'), '-f', '0.3', '-n', '-m', '-R'])
        shutil.move(src=join(temp_dir, 'bet_mask.nii.gz'), dst=outfile)
        shutil.rmtree(temp_dir)
    else:
        logging.warning(
            'No example_func was found in the Feat directory.' +
            ' A mask will be created including all voxels with varying intensity over time in the fMRI data.' +
            ' Please check!'
        )
        call([FSLMATHS, infile, '-Tstd', '-bin', outfile])


def run_aroma(infile, outdir, mask, dim, t_r, melodic_dir, affmat, warp, mc, denoise_type, seed=None, verbose=True):
    """Run aroma denoising.

    Parameters
    ----------
    infile: str
        Input data file (nii.gz) to be denoised
    outdir: str
        Output directory
    mask: str
        Mask file to be applied during MELODIC
    dim: int
        Dimensionality of ICA
    t_r: float
        Repetition Time (in seconds) of the fMRI data
    existing_melodic_dir: str
        MELODIC directory in case it has been run before, otherwise define empty string
    affmat: str
        Mat file describing the linear registration to structural space (if image still in native space)
    warp: str
        Warp file describing the non-linear registration to MNI152 space (if image not yet in MNI space)
    mc: str
        Text file containing the realignment parameters
    denoise_type: str
        Type of requested denoising ('aggr', 'nonaggr', 'both', 'none')
    seed: Optional(int)
        Seed for both MELODIC and python RNGs
    verbose: Optional(bool)
        Log info messages and save classification to text files in output directory

    Returns
    -------
    None

    Output (within the requested output directory)
    ------
    A nii.gz file of the denoised fMRI data (denoised_func_data_<denoise_type>.nii.gz) in outdir
    """

    assert isfile(infile)
    assert is_writable_directory(outdir)
    assert isfile(mask)
    assert 0 <= dim < 100
    assert 0.5 <= t_r < 10
    assert melodic_dir is None or isdir(melodic_dir)
    assert affmat is None or isfile(affmat)
    assert warp is None or isfile(warp)
    assert isfile(mc)
    assert denoise_type in ['none', 'aggr', 'nonaggr', 'both']

    logging.info("------------------------------- RUNNING ICA-AROMA ------------------------------- ")
    logging.info("--------------- 'ICA-based Automatic Removal Of Motion Artefacts' --------------- ")

    tempdir = mkdtemp(prefix='run_aroma')

    logging.info('Step 1) MELODIC')
    melodic_ics_file = join(tempdir, 'thresholded_ics.nii.gz')
    mix, ftmix = run_ica(infile, outfile=melodic_ics_file, maskfile=mask, t_r=t_r, ndims_ica=dim, melodic_indir=melodic_dir, seed=seed)

    logging.info('Step 2) Automatic classification of the components')
    logging.info('  - registering the spatial maps to MNI')
    melodic_ics_file_mni = join(tempdir, 'melodic_IC_thr_MNI2mm.nii.gz')
    register_to_mni(melodic_ics_file, melodic_ics_file_mni, affmat=affmat, warp=warp)

    logging.info('  - extracting the CSF & Edge fraction features')
    edge_fraction, csf_fraction = feature_spatial(melodic_ics_file_mni)

    logging.info('  - extracting the Maximum RP correlation feature')
    max_rp_correl = feature_time_series(mix=mix, rparams=np.loadtxt(mc), seed=seed)

    logging.info('  - extracting the High-frequency content feature')
    hfc = feature_frequency(ftmix, t_r=t_r)

    logging.info('  - classification')
    motion_ic_indices = classification(max_rp_correl, edge_fraction, hfc, csf_fraction)

    logging.info('Step 3) Data denoising')
    if denoise_type in ['nonaggr', 'aggr', 'both']:
        if denoise_type in ['nonaggr', 'both']:
            outfile = join(outdir, 'denoised_func_data_nonaggr.nii.gz')
            denoising(infile, outfile, mix, motion_ic_indices, aggressive=False)
        if denoise_type in ['aggr', 'both']:
            outfile = join(outdir, 'denoised_func_data_aggr.nii.gz')
            denoising(infile, outfile, mix, motion_ic_indices, aggressive=True)

    shutil.rmtree(tempdir)

    if verbose:
        save_classification(outdir, max_rp_correl, edge_fraction, hfc, csf_fraction, motion_ic_indices)


if __name__ == '__main__':

    args = parse_cmdline(sys.argv[1:])

    level = getattr(logging, args.loglevel.upper(), None)
    if level is not None:
        print('Logging Level is %s (%d)' % (args.loglevel, level))
        logging.basicConfig(level=level)

    using_feat = args.featdir is not None
    if using_feat:
        featdir = args.featdir
    try:
        infile, mc, affmat, warp, melodic_dir = feat_args(args) if using_feat else nonfeat_args(args)
        outdir, dim, denoise_type, existing_mask, seed = common_args(args)
    except ValueError as exception:
        print('%s' % exception, file=sys.stderr)
        sys.exit(1)

    # Create output directory if needed
    if not exists(outdir):
        try:
            os.makedirs(outdir)
        except OSError as exception:
            logging.critical(
                "Output directory %s doesn't exist and can't create it (%s). Exiting ...",
                outdir, str(exception)
            )
            sys.exit(1)

    # Get TR of the fMRI data, if not specified and check
    TR = args.TR if args.TR is not None else nifti_pixdims(infile)[3]
    if TR == 1.0:
        logging.warning('TR is exactly 1.0 secs. Please check whether this is correct')
    elif TR == 0.0:
        logging.critical(
            'TR is exactly zero secs. ICA-AROMA requires a valid TR.' +
            ' Check the header, or define the TR as an additional argument.' +
            ' Exiting ...'
        )
        sys.exit(1)

    mask = join(outdir, 'mask.nii.gz')
    if existing_mask is not None:
        shutil.copyfile(src=existing_mask, outfile=mask)
    elif using_feat:
        create_mask(infile, outfile=mask, featdir=featdir)
    else:
        create_mask(infile, outfile=mask)

    run_aroma(
        infile=infile,
        outdir=outdir,
        mask=mask,
        dim=dim,
        t_r=TR,
        melodic_dir=melodic_dir,
        affmat=affmat,
        warp=warp,
        mc=mc,
        denoise_type=denoise_type,
        seed=seed
    )
