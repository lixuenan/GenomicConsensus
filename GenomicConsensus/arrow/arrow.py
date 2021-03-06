# Authors: David Alexander, Lance Hepler
from __future__ import absolute_import, division, print_function

import logging, os.path
import ConsensusCore2 as cc, numpy as np

from .. import reference
from ..options import options
from ..Worker import WorkerProcess, WorkerThread
from ..ResultCollector import ResultCollectorProcess, ResultCollectorThread

from GenomicConsensus.consensus import Consensus, ArrowConsensus, join
from GenomicConsensus.windows import kSpannedIntervals, holes, subWindow
from GenomicConsensus.variants import annotateVariants
from GenomicConsensus.arrow import diploid
from GenomicConsensus.utils import die

import GenomicConsensus.arrow.model as M
import GenomicConsensus.arrow.utils as U

def consensusAndVariantsForWindow(alnFile, refWindow, referenceContig,
                                  depthLimit, arrowConfig):
    """
    High-level routine for calling the consensus for a
    window of the genome given a BAM file.

    Identifies the coverage contours of the window in order to
    identify subintervals where a good consensus can be called.
    Creates the desired "no evidence consensus" where there is
    inadequate coverage.
    """
    winId, winStart, winEnd = refWindow
    logging.info("Arrow operating on %s" %
                 reference.windowToString(refWindow))

    if options.fancyChunking:
        # 1) identify the intervals with adequate coverage for arrow
        #    consensus; restrict to intervals of length > 10
        alnHits = U.readsInWindow(alnFile, refWindow,
                                  depthLimit=20000,
                                  minMapQV=arrowConfig.minMapQV,
                                  strategy="long-and-strand-balanced",
                                  stratum=options.readStratum,
                                  barcode=options.barcode)
        starts = np.fromiter((hit.tStart for hit in alnHits), np.int)
        ends   = np.fromiter((hit.tEnd   for hit in alnHits), np.int)
        intervals = kSpannedIntervals(refWindow, arrowConfig.minPoaCoverage,
                                      starts, ends, minLength=10)
        coverageGaps = holes(refWindow, intervals)
        allIntervals = sorted(intervals + coverageGaps)
        if len(allIntervals) > 1:
            logging.info("Usable coverage in %s: %r" %
                         (reference.windowToString(refWindow), intervals))

    else:
        allIntervals = [ (winStart, winEnd) ]

    # 2) pull out the reads we will use for each interval
    # 3) call consensusForAlignments on the interval
    subConsensi = []
    variants = []

    for interval in allIntervals:
        intStart, intEnd = interval
        intRefSeq = referenceContig[intStart:intEnd]
        subWin = subWindow(refWindow, interval)

        windowRefSeq = referenceContig[intStart:intEnd]
        alns = U.readsInWindow(alnFile, subWin,
                               depthLimit=depthLimit,
                               minMapQV=arrowConfig.minMapQV,
                               strategy="long-and-strand-balanced",
                               stratum=options.readStratum,
                               barcode=options.barcode)
        clippedAlns_ = [ aln.clippedTo(*interval) for aln in alns ]
        clippedAlns = U.filterAlns(subWin, clippedAlns_, arrowConfig)

        if len([ a for a in clippedAlns
                 if a.spansReferenceRange(*interval) ]) >= arrowConfig.minPoaCoverage:

            logging.debug("%s: Reads being used: %s" %
                          (reference.windowToString(subWin),
                           " ".join([str(hit.readName) for hit in alns])))

            alnsUsed = [] if options.reportEffectiveCoverage else None
            css = U.consensusForAlignments(subWin,
                                           intRefSeq,
                                           clippedAlns,
                                           arrowConfig,
                                           alnsUsed=alnsUsed)

            # Tabulate the coverage implied by these alignments, as
            # well as the post-filtering ("effective") coverage
            siteCoverage = U.coverageInWindow(subWin, alns)
            effectiveSiteCoverage = U.coverageInWindow(subWin, alnsUsed) if options.reportEffectiveCoverage else None

            variants_, newPureCss = U.variantsFromConsensus(subWin, windowRefSeq, css.sequence, css.confidence,
                                                            siteCoverage, effectiveSiteCoverage,
                                                            options.aligner, ai=None,
                                                            diploid=arrowConfig.polishDiploid)

            # Annotate?
            if options.annotateGFF:
                annotateVariants(variants_, clippedAlns)

            variants += variants_

            # The nascent consensus sequence might contain ambiguous bases, these
            # need to be removed as software in the wild cannot deal with such
            # characters and we only use IUPAC for *internal* bookkeeping.
            if arrowConfig.polishDiploid:
                css.sequence = newPureCss
        else:
            css = ArrowConsensus.noCallConsensus(arrowConfig.noEvidenceConsensus,
                                                 subWin, intRefSeq)
        subConsensi.append(css)

    # 4) glue the subwindow consensus objects together to form the
    #    full window consensus
    css = join(subConsensi)

    # 5) Return
    return css, variants


class ArrowWorker(object):

    @property
    def arrowConfig(self):
        return self._algorithmConfig

    def onChunk(self, workChunk):
        referenceWindow  = workChunk.window
        refId, refStart, refEnd = referenceWindow

        refSeqInWindow = reference.sequenceInWindow(referenceWindow)

        # Quick cutout for no-coverage case
        if not workChunk.hasCoverage:
            noCallCss = ArrowConsensus.noCallConsensus(self.arrowConfig.noEvidenceConsensus,
                                                       referenceWindow, refSeqInWindow)
            return (referenceWindow, (noCallCss, []))

        # General case
        eWindow = reference.enlargedReferenceWindow(referenceWindow,
                                                    options.referenceChunkOverlap)
        _, eStart, eEnd = eWindow

        # We call consensus on the enlarged window and then map back
        # to the reference and clip the consensus at the implied
        # bounds.  This seems to be more reliable thank cutting the
        # consensus bluntly
        refContig = reference.byName[refId].sequence
        refSequenceInEnlargedWindow = refContig[eStart:eEnd]

        #
        # Get the consensus for the enlarged window.
        #
        css_, variants_ = \
            consensusAndVariantsForWindow(self._inAlnFile, eWindow,
                                          refContig, options.coverage, self.arrowConfig)

        #
        # Restrict the consensus and variants to the reference window.
        #
        ga = cc.Align(refSequenceInEnlargedWindow, css_.sequence)
        targetPositions = cc.TargetToQueryPositions(ga)
        cssStart = targetPositions[refStart-eStart]
        cssEnd   = targetPositions[refEnd-eStart]

        cssSequence    = css_.sequence[cssStart:cssEnd]
        cssQv          = css_.confidence[cssStart:cssEnd]
        variants       = [ v for v in variants_
                           if refStart <= v.refStart < refEnd ]

        consensusObj = Consensus(referenceWindow,
                                 cssSequence,
                                 cssQv)

        return (referenceWindow, (consensusObj, variants))



#
# Slave process/thread classes
#
class ArrowWorkerProcess(ArrowWorker, WorkerProcess): pass
class ArrowWorkerThread(ArrowWorker, WorkerThread): pass


#
# Plugin API
#
__all__ = [ "name",
            "availability",
            "configure",
            "slaveFactories" ]

name = "arrow"
availability = (True, "OK")

def configure(options, alnFile):
    if alnFile.readType != "standard":
        raise U.IncompatibleDataException(
            "The Arrow algorithm requires a BAM file containing standard (non-CCS) reads." )

    if options.diploid:
        logging.info(
            "Diploid polishing in the Arrow model is in *BETA* mode.\n"
            "Any multi-base string that appears in annotation files\n"
            "is not phased!")

    # load parameters from file
    if options.parametersFile:
        logging.info("Loading model parameters from: ({0})".format(options.parametersFile))
        if not cc.LoadModels(options.parametersFile):
            die("Arrow: unable to load parameters from: ({0})".format(options.parametersFile))

    # test available chemistries
    supp = set(cc.SupportedChemistries())
    logging.info("Found consensus models for: ({0})".format(", ".join(sorted(supp))))

    used = set([])
    if options.parametersSpec != "auto":
        logging.info("Overriding model selection with: ({0})".format(options.parametersSpec))
        if not cc.OverrideModel(options.parametersSpec):
            die("Arrow: unable to override model with: ({0})".format(options.parametersSpec))
        used.add(options.parametersSpec)
    else:
        used.update(alnFile.sequencingChemistry)
        unsupp = used - supp
        if used - supp:
            die("Arrow: unsupported chemistries found: ({0})".format(", ".join(sorted(unsupp))))

    # All arrow models require PW except P6 and the first S/P1-C1
    for readGroup in alnFile.readGroupTable:
        if set([readGroup["SequencingChemistry"]]) - set(["P6-C4", "S/P1-C1/beta"]):
            if ("Ipd" not in readGroup["BaseFeatures"] or
                "PulseWidth" not in readGroup["BaseFeatures"]):
                die("Arrow model requires missing base feature: IPD or PulseWidth")

    logging.info("Using consensus models for: ({0})".format(", ".join(sorted(used))))

    return M.ArrowConfig(minMapQV=options.minMapQV,
                         noEvidenceConsensus=options.noEvidenceConsensusCall,
                         computeConfidence=(not options.fastMode),
                         minReadScore=options.minReadScore,
                         minHqRegionSnr=options.minHqRegionSnr,
                         minZScore=options.minZScore,
                         minAccuracy=options.minAccuracy,
                         maskRadius=options.maskRadius,
                         maskErrorRate=options.maskErrorRate,
                         polishDiploid=options.diploid)

def slaveFactories(threaded):
    # By default we use slave processes. The tuple ordering is important.
    if threaded:
        return (ArrowWorkerThread,  ResultCollectorThread)
    else:
        return (ArrowWorkerProcess, ResultCollectorProcess)
