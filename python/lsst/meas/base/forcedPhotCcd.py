#!/usr/bin/env python
#
# LSST Data Management System
# Copyright 2008-2015 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
import collections

import lsst.pex.config
import lsst.pex.exceptions
from lsst.log import Log
import lsst.pipe.base
import lsst.geom
import lsst.afw.geom
import lsst.afw.image
import lsst.afw.table
import lsst.sphgeom

from .forcedPhotImage import ForcedPhotImageTask, ForcedPhotImageConfig

try:
    from lsst.meas.mosaic import applyMosaicResults
except ImportError:
    applyMosaicResults = None

__all__ = ("PerTractCcdDataIdContainer", "ForcedPhotCcdConfig", "ForcedPhotCcdTask", "imageOverlapsTract")


class PerTractCcdDataIdContainer(lsst.pipe.base.DataIdContainer):
    """A data ID container which combies raw data IDs with a tract.

    Notes
    -----
    Required because we need to add "tract" to the raw data ID keys (defined as
    whatever we use for ``src``) when no tract is provided (so that the user is
    not required to know which tracts are spanned by the raw data ID).

    This subclass of `~lsst.pipe.base.DataIdContainer` assumes that a calexp is
    being measured using the detection information, a set of reference
    catalogs, from the set of coadds which intersect with the calexp.  It needs
    the calexp id (e.g.  visit, raft, sensor), but is also uses the tract to
    decide what set of coadds to use.  The references from the tract whose
    patches intersect with the calexp are used.
    """

    def makeDataRefList(self, namespace):
        """Make self.refList from self.idList
        """
        if self.datasetType is None:
            raise RuntimeError("Must call setDatasetType first")
        log = Log.getLogger("meas.base.forcedPhotCcd.PerTractCcdDataIdContainer")
        skymap = None
        visitTract = collections.defaultdict(set)   # Set of tracts for each visit
        visitRefs = collections.defaultdict(list)   # List of data references for each visit
        for dataId in self.idList:
            if "tract" not in dataId:
                # Discover which tracts the data overlaps
                log.info("Reading WCS for components of dataId=%s to determine tracts", dict(dataId))
                if skymap is None:
                    skymap = namespace.butler.get(namespace.config.coaddName + "Coadd_skyMap")

                for ref in namespace.butler.subset("calexp", dataId=dataId):
                    if not ref.datasetExists("calexp"):
                        continue

                    visit = ref.dataId["visit"]
                    visitRefs[visit].append(ref)

                    md = ref.get("calexp_md", immediate=True)
                    wcs = lsst.afw.geom.makeSkyWcs(md)
                    box = lsst.geom.Box2D(lsst.afw.image.bboxFromMetadata(md))
                    # Going with just the nearest tract.  Since we're throwing all tracts for the visit
                    # together, this shouldn't be a problem unless the tracts are much smaller than a CCD.
                    tract = skymap.findTract(wcs.pixelToSky(box.getCenter()))
                    if imageOverlapsTract(tract, wcs, box):
                        visitTract[visit].add(tract.getId())
            else:
                self.refList.extend(ref for ref in namespace.butler.subset(self.datasetType, dataId=dataId))

        # Ensure all components of a visit are kept together by putting them all in the same set of tracts
        for visit, tractSet in visitTract.items():
            for ref in visitRefs[visit]:
                for tract in tractSet:
                    self.refList.append(namespace.butler.dataRef(datasetType=self.datasetType,
                                                                 dataId=ref.dataId, tract=tract))
        if visitTract:
            tractCounter = collections.Counter()
            for tractSet in visitTract.values():
                tractCounter.update(tractSet)
            log.info("Number of visits for each tract: %s", dict(tractCounter))


def imageOverlapsTract(tract, imageWcs, imageBox):
    """Return whether the given bounding box overlaps the tract given a WCS.

    Parameters
    ----------
    tract:
        TractInfo specifying a tract.
    imageWcs: `lsst.afw.geom.SkyWcs`
        World coordinate system for the image.
    imageBox:
        Bounding box for the image.

    Returns
    -------
    overlap : `bool`
        ``True`` if the bounding box overlaps the tract; ``False`` otherwise.
    """
    tractPoly = tract.getOuterSkyPolygon()

    imagePixelCorners = lsst.geom.Box2D(imageBox).getCorners()
    try:
        imageSkyCorners = imageWcs.pixelToSky(imagePixelCorners)
    except lsst.pex.exceptions.LsstCppException as e:
        # Protecting ourselves from awful Wcs solutions in input images
        if (not isinstance(e.message, lsst.pex.exceptions.DomainErrorException) and
                not isinstance(e.message, lsst.pex.exceptions.RuntimeErrorException)):
            raise
        return False

    imagePoly = lsst.sphgeom.ConvexPolygon.convexHull([coord.getVector() for coord in imageSkyCorners])
    return tractPoly.intersects(imagePoly)  # "intersects" also covers "contains" or "is contained by"


class ForcedPhotCcdConfig(ForcedPhotImageConfig):
    doApplyUberCal = lsst.pex.config.Field(
        dtype=bool,
        doc="Apply meas_mosaic ubercal results to input calexps?",
        default=False
    )

class ForcedPhotCcdTask(ForcedPhotImageTask):
    """A command-line driver for performing forced measurement on CCD images.

    Notes
    -----
    This task is a subclass of
    :lsst-task:`lsst.meas.base.forcedPhotImage.ForcedPhotImageTask` which is
    specifically for doing forced measurement on a single CCD exposure, using
    as a reference catalog the detections which were made on overlapping
    coadds.

    The `run` method (inherited from ``ForcedPhotImageTask``) takes a
    `~lsst.daf.persistence.ButlerDataRef` argument that corresponds to a single
    CCD. This should contain the data ID keys that correspond to the
    ``forced_src`` dataset (the output dataset for this task), which are
    typically all those used to specify the ``calexp`` dataset (``visit``,
    ``raft``, ``sensor`` for LSST data) as well as a coadd tract. The tract is
    used to look up the appropriate coadd measurement catalogs to use as
    references (e.g. ``deepCoadd_src``; see
    :lsst-task:`lsst.meas.base.references.CoaddSrcReferencesTask` for more
    information). While the tract must be given as part of the dataRef, the
    patches are determined automatically from the bounding box and WCS of the
    calexp to be measured, and the filter used to fetch references is set via
    the ``filter`` option in the configuration of
    :lsst-task:`lsst.meas.base.references.BaseReferencesTask`).

    In addition to the `run` method, ForcedPhotCcdTask overrides several
    methods of ``ForcedPhotImageTask`` to specialize it for single-CCD
    processing, including ``makeIdFactory``, ``fetchReferences``, and
    ``getExposure``. None of these should be called directly by the user,
    though it may be useful to override them further in subclasses.
    """

    ConfigClass = ForcedPhotCcdConfig
    RunnerClass = lsst.pipe.base.ButlerInitializedTaskRunner
    _DefaultName = "forcedPhotCcd"
    dataPrefix = ""

    def makeIdFactory(self, dataRef):
        """Create an object that generates globally unique source IDs.

        Source IDs are created based on a per-CCD ID and the ID of the CCD
        itself.

        Parameters
        ----------
        dataRef : `lsst.daf.persistence.ButlerDataRef`
            Butler data reference. The ``ccdExposureId_bits`` and
            ``ccdExposureId`` datasets are accessed. The data ID must have the
            keys that correspond to ``ccdExposureId``, which are generally the
            same as those that correspond to ``calexp`` (``visit``, ``raft``,
            ``sensor`` for LSST data).
        """
        expBits = dataRef.get("ccdExposureId_bits")
        expId = int(dataRef.get("ccdExposureId"))
        return lsst.afw.table.IdFactory.makeSource(expId, 64 - expBits)

    def getExposureId(self, dataRef):
        return int(dataRef.get("ccdExposureId", immediate=True))

    def fetchReferences(self, dataRef, exposure):
        """Get sources that overlap the exposure

        Parameters
        ----------
        dataRef : `lsst.daf.persistence.ButlerDataRef`
            Butler data reference corresponding to the image to be measured;
            should have ``tract``, ``patch``, and ``filter`` keys.

        exposure : `lsst.afw.image.Exposure`
            The image to be measured (used only to obtain a WCS and bounding
            box).

        Returns
        -------
        lsst.afw.table.SourceCatalog
            Catalog of sources that overlap the exposure

        Notes
        -----
        The returned catalog is sorted by ID and guarantees that all included
        children have their parent included and that all Footprints are valid.

        All work is delegated to the references subtask; see
        :lsst-task:`lsst.meas.base.references.CoaddSrcReferencesTask`
        for information about the default behavior.
        """
        references = lsst.afw.table.SourceCatalog(self.references.schema)
        badParents = set()
        unfiltered = self.references.fetchInBox(dataRef, exposure.getBBox(), exposure.getWcs())
        for record in unfiltered:
            if record.getFootprint() is None or record.getFootprint().getArea() == 0:
                if record.getParent() != 0:
                    self.log.warn("Skipping reference %s (child of %s) with bad Footprint",
                                  record.getId(), record.getParent())
                else:
                    self.log.warn("Skipping reference parent %s with bad Footprint", record.getId())
                    badParents.add(record.getId())
            elif record.getParent() not in badParents:
                references.append(record)
        # catalog must be sorted by parent ID for lsst.afw.table.getChildren to work
        references.sort(lsst.afw.table.SourceTable.getParentKey())
        return references

    def getExposure(self, dataRef):
        """Read input exposure for measurement.

        Parameters
        ----------
        dataRef : `lsst.daf.persistence.ButlerDataRef`
            Butler data reference. Only the ``calexp`` dataset is used, unless
            ``config.doApplyUberCal`` is ``True``, in which case the
            corresponding meas_mosaic outputs are used as well.
        """
        exposure = ForcedPhotImageTask.getExposure(self, dataRef)
        if not self.config.doApplyUberCal:
            return exposure
        if applyMosaicResults is None:
            raise RuntimeError(
                "Cannot use improved calibrations for %s because meas_mosaic could not be imported."
                % (dataRef.dataId,))
        else:
            applyMosaicResults(dataRef, calexp=exposure)
        return exposure

    def _getConfigName(self):
        # Documented in superclass.
        return self.dataPrefix + "forcedPhotCcd_config"

    def _getMetadataName(self):
        # Documented in superclass
        return self.dataPrefix + "forcedPhotCcd_metadata"

    @classmethod
    def _makeArgumentParser(cls):
        parser = lsst.pipe.base.ArgumentParser(name=cls._DefaultName)
        parser.add_id_argument("--id", "forced_src", help="data ID with raw CCD keys [+ tract optionally], "
                               "e.g. --id visit=12345 ccd=1,2 [tract=0]",
                               ContainerClass=PerTractCcdDataIdContainer)
        return parser
