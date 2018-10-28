#
# LSST Data Management System
# Copyright 2015 LSST Corporation.
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
# see <http://www.lsstcorp.org/LegalNotices/>.
#
import math

import numpy as np

import lsst.pex.config
import lsst.pex.exceptions
import lsst.afw.image
import lsst.pipe.base
from .apCorrRegistry import getApCorrNameSet

# If True then scale instFlux error by apCorr; if False then use a more complex computation
# that over-estimates instFlux error (often grossly so) because it double-counts photon noise.
# This flag is intended to be temporary until we figure out a better way to compute
# the effects of aperture correction on instFlux uncertainty
UseNaiveFluxErr = True

__all__ = ("ApplyApCorrConfig", "ApplyApCorrTask")


class ApCorrInfo:
    """Information about fields and keys required for aperture correction.

    Parameters
    ----------
    schema : `lsst.afw.table`
        Source catalog schema. Three fields are used to generate keys:
        - {name}_instFlux
        - {name}_instFluxErr
        - {name}_flag
        Three fields are added:
        - {name}_apCorr (only if not already added by proxy)
        - {name}_apCorrErr (only if not already added by proxy)
        - {name}_flag_apCorr
    model: `str`
        Field name prefix for instFlux with aperture correction model, e.g.
        "base_PsfFlux"
    name: `str`
        Field name prefix for instFlux needing aperture correction; may be
        ``None`` if it is the same as ``model``

    Attributes
    ----------
    name : `str`
        Field name prefix for flux needing aperture correction
    modelName : `str`
        Field name for aperture correction model for flux
    modelSigmaName : `str`
        Field name for aperture correction model for fluxErr
    doApCorrColumn : `bool`
        Should we write the aperture correction values? (not if they're
        already being written by a proxy)
    instFluxName : `str`
        Name of instFlux field
    instFluxErrName : `str`
        Name of instFlux sigma field
    instFluxKey
        Key to instFlux field
    instFluxErrKey
        Key to instFlux sigma field
    fluxFlagKey
        Key to flux flag field
    apCorrKey
        Key to new aperture correction field
    apCorrErrKey
        Key to new aperture correction sigma field
    apCorrFlagKey
        Key to new aperture correction flag field

    Notes
    -----
    The aperture correction can be derived from the meaasurements in the
    column being aperture-corrected or from measurements in a different
    column (a "proxy"). In the first case, we will add columns to contain
    the aperture correction values; in the second case (using a proxy),
    we will add an alias to the proxy's aperture correction values. In
    all cases, we add a flag.
    """

    def __init__(self, schema, model, name=None):
        if name is None:
            name = model
        self.name = name
        self.modelName = model + "_instFlux"
        self.modelSigmaName = model + "_instFluxErr"
        self.instFluxName = name + "_instFlux"
        self.instFluxErrName = name + "_instFluxErr"
        self.instFluxKey = schema.find(self.instFluxName).key
        self.instFluxErrKey = schema.find(self.instFluxErrName).key
        self.fluxFlagKey = schema.find(name + "_flag").key

        # No need to write the same aperture corrections multiple times
        self.doApCorrColumn = (name == model or model + "_apCorr" not in schema)
        if self.doApCorrColumn:
            self.apCorrKey = schema.addField(
                name + "_apCorr",
                doc="aperture correction applied to %s" % (name,),
                type=np.float64,
            )
            self.apCorrErrKey = schema.addField(
                name + "_apCorrErr",
                doc="standard deviation of aperture correction applied to %s" % (name,),
                type=np.float64,
            )
        else:
            aliases = schema.getAliasMap()
            aliases.set(name + "_apCorr", model + "_apCorr")
            aliases.set(name + "_apCorrErr", model + "_apCorrErr")
            self.apCorrKey = schema.find(name + "_apCorr").key
            self.apCorrErrKey = schema.find(name + "_apCorrErr").key

        self.apCorrFlagKey = schema.addField(
            name + "_flag_apCorr",
            doc="set if unable to aperture correct %s" % (name,),
            type="Flag",
        )


class ApplyApCorrConfig(lsst.pex.config.Config):
    ignoreList = lsst.pex.config.ListField(
        doc="flux measurement algorithms in getApCorrNameSet() to ignore; "
            "if a name is listed that does not appear in getApCorrNameSet() then a warning is logged",
        dtype=str,
        optional=False,
        default=(),
    )
    doFlagApCorrFailures = lsst.pex.config.Field(
        doc="set the general failure flag for a flux when it cannot be aperture-corrected?",
        dtype=bool,
        default=True,
    )
    proxies = lsst.pex.config.DictField(
        doc="flux measurement algorithms to be aperture-corrected by reference to another algorithm; "
            "this is a mapping alg1:alg2, where 'alg1' is the algorithm being corrected, and 'alg2' "
            "is the algorithm supplying the corrections",
        keytype=str,
        itemtype=str,
        default={},
    )


class ApplyApCorrTask(lsst.pipe.base.Task):
    """Apply aperture corrections.

    Parameters
    ----------
    schema : `lsst.afw.table.Schema`
    """
    ConfigClass = ApplyApCorrConfig
    _DefaultName = "applyApCorr"

    def __init__(self, schema, **kwds):
        lsst.pipe.base.Task.__init__(self, **kwds)

        self.apCorrInfoDict = dict()
        apCorrNameSet = getApCorrNameSet()
        ignoreSet = set(self.config.ignoreList)
        missingNameSet = ignoreSet - set(apCorrNameSet)
        if missingNameSet:
            self.log.warn("Fields in ignoreList that are not in fluxCorrectList: %s",
                          sorted(list(missingNameSet)))
        for name in apCorrNameSet - ignoreSet:
            if name + "_instFlux" not in schema:
                # if a field in the registry is missing from the schema, silently ignore it.
                continue
            self.apCorrInfoDict[name] = ApCorrInfo(schema=schema, model=name)

        for name, model in self.config.proxies.items():
            if name in apCorrNameSet:
                # Already done or ignored
                continue
            if name + "_instFlux" not in schema:
                # Silently ignore
                continue
            self.apCorrInfoDict[name] = ApCorrInfo(schema=schema, model=model, name=name)

    def run(self, catalog, apCorrMap):
        """Apply aperture corrections to a catalog of sources.

        Parameters
        ----------
        catalog :
            Catalog of sources. Will be updated in place.
        apCorrMap : `lsst.afw.image.ApCorrMap`
            Aperture correction map

        Notes
        -----
        If you show debug-level log messages then you will see statistics for
        the effects of aperture correction.
        """
        self.log.info("Applying aperture corrections to %d instFlux fields", len(self.apCorrInfoDict))
        if UseNaiveFluxErr:
            self.log.debug("Use naive instFlux sigma computation")
        else:
            self.log.debug("Use complex instFlux sigma computation that double-counts photon noise "
                           "and thus over-estimates instFlux uncertainty")
        for apCorrInfo in self.apCorrInfoDict.values():
            apCorrModel = apCorrMap.get(apCorrInfo.modelName)
            apCorrErrModel = apCorrMap.get(apCorrInfo.modelSigmaName)
            if None in (apCorrModel, apCorrErrModel):
                missingNames = [(apCorrInfo.modelName, apCorrInfo.modelSigmaName)[i]
                                for i, model in enumerate((apCorrModel, apCorrErrModel)) if model is None]
                self.log.warn("Cannot aperture correct %s because could not find %s in apCorrMap" %
                              (apCorrInfo.name, " or ".join(missingNames),))
                for source in catalog:
                    source.set(apCorrInfo.apCorrFlagKey, True)
                continue

            for source in catalog:
                center = source.getCentroid()
                # say we've failed when we start; we'll unset these flags when we succeed
                source.set(apCorrInfo.apCorrFlagKey, True)
                oldFluxFlagState = False
                if self.config.doFlagApCorrFailures:
                    oldFluxFlagState = source.get(apCorrInfo.fluxFlagKey)
                    source.set(apCorrInfo.fluxFlagKey, True)

                apCorr = 1.0
                apCorrErr = 0.0
                try:
                    apCorr = apCorrModel.evaluate(center)
                    if not UseNaiveFluxErr:
                        apCorrErr = apCorrErrModel.evaluate(center)
                except lsst.pex.exceptions.DomainError:
                    continue

                if apCorrInfo.doApCorrColumn:
                    source.set(apCorrInfo.apCorrKey, apCorr)
                    source.set(apCorrInfo.apCorrErrKey, apCorrErr)

                if apCorr <= 0.0 or apCorrErr < 0.0:
                    continue

                instFlux = source.get(apCorrInfo.instFluxKey)
                instFluxErr = source.get(apCorrInfo.instFluxErrKey)
                source.set(apCorrInfo.instFluxKey, instFlux*apCorr)
                if UseNaiveFluxErr:
                    source.set(apCorrInfo.instFluxErrKey, instFluxErr*apCorr)
                else:
                    a = instFluxErr/instFlux
                    b = apCorrErr/apCorr
                    source.set(apCorrInfo.instFluxErrKey, abs(instFlux*apCorr)*math.sqrt(a*a + b*b))
                source.set(apCorrInfo.apCorrFlagKey, False)
                if self.config.doFlagApCorrFailures:
                    source.set(apCorrInfo.fluxFlagKey, oldFluxFlagState)

            if self.log.getLevel() <= self.log.DEBUG:
                # log statistics on the effects of aperture correction
                apCorrArr = np.array([s.get(apCorrInfo.apCorrKey) for s in catalog])
                apCorrErrArr = np.array([s.get(apCorrInfo.apCorrErrKey) for s in catalog])
                self.log.debug("For instFlux field %r: mean apCorr=%s, stdDev apCorr=%s, "
                               "mean apCorrErr=%s, stdDev apCorrErr=%s for %s sources",
                               apCorrInfo.name, apCorrArr.mean(), apCorrArr.std(),
                               apCorrErrArr.mean(), apCorrErrArr.std(), len(catalog))
