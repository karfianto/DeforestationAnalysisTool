#encoding: utf-8

import logging
import settings
import simplejson as json
import collections
import urllib
import re
import time

from earthengine.connector import EarthEngine
import ee

from time_utils import timestamp
from datetime import timedelta, date

METER2_TO_KM2 = 1.0/(1000*1000)

CALL_SCOPE = "SAD"
#CALL_SCOPE = "sad_test"
KRIGING = "kriging/com.google.earthengine.examples.kriging.KrigedModisImage"

ee.data.DEFAULT_DEADLINE = 600
ee.Initialize(settings.EE_CREDENTIALS)


class Stats(object):
    DEFORESTATION = 7
    DEGRADATION = 8

    def _paint(self, current_asset, report_id, table, value):
        fc = ee.FeatureCollection(int(table))
        fc = fc.filterMetadata('report_id', 'equals', int(report_id))
        fc = fc.filterMetadata('type', 'equals', value)
        return current_asset.paint(fc, value)

    def _get_historical_freeze(self, report_id, frozen_image):
        remapped = frozen_image.remap([0,1,2,3,4,5,6,7,8,9],
                                      [0,1,2,3,4,5,6,1,1,9])
        def_image = self._paint(remapped, report_id, settings.FT_TABLE_ID, 7)
        deg_image = self._paint(def_image, report_id, settings.FT_TABLE_ID, 8)
        return deg_image.select(['remapped'], ['class'])

    def _get_area(self, report_id, image_id, polygons):
        freeze = self._get_historical_freeze(report_id, ee.Image(image_id))
        return _get_area_histogram(
            freeze, polygons, [Stats.DEFORESTATION, Stats.DEGRADATION])

    def get_stats_for_polygon(self, assetids, polygon):
        """ example polygon, must be CCW
            #polygon = [[[-61.9,-11.799],[-61.9,-11.9],[-61.799,-11.9],[-61.799,-11.799],[-61.9,-11.799]]]
        """
        feature = ee.Feature(ee.Feature.Polygon(polygon), {'name': 'myPoly'})
        polygons = ee.FeatureCollection([ee.Feature(feature)])

        # javascript way, lovely
        if not hasattr(assetids, '__iter__'):
            assetids = [assetids]

        reports = []
        for report_id, asset_id in assetids:
            result = self._get_area(report_id, asset_id, polygons)
            if result is None: return None
            reports.append(result[0])

        stats = []
        for x in reports:
            stats.append({
                'total_area': x['total']['area'] * METER2_TO_KM2,
                'def': x[str(Stats.DEFORESTATION)]['area'] * METER2_TO_KM2,
                'deg': x[str(Stats.DEGRADATION)]['area'] * METER2_TO_KM2,
            })
        return stats

    def get_stats(self, report_id, frozen_image, table_id):
        result = self._get_area(
            report_id, frozen_image, ee.FeatureCollection(int(table_id)))
        if result is None: return None
        stats = {}
        for x in result:
            name = x['name']
            if isinstance(name, float): name = int(name)
            stats['%s_%s' % (table_id, name)] = {
                'id': str(name),
                'table': table_id,
                'total_area': x['total']['area'] * METER2_TO_KM2,
                'def': x[str(Stats.DEFORESTATION)]['area'] * METER2_TO_KM2,
                'deg': x[str(Stats.DEGRADATION)]['area'] * METER2_TO_KM2,
            }

        return stats


class EELandsat(object):
    def list(self, bounds, params={}):
        bbox = ee.Feature.Rectangle(
            *[float(i.strip()) for i in bounds.split(',')])
        images = ee.ImageCollection('L7_L1T').filterBounds(bbox).getInfo()
        logging.info(images)
        if 'features' in images:
            return [x['id'] for x in images['features']]
        return []

    def mapid(self, start, end):
        MAP_IMAGE_BANDS = ['30','20','10']
        PREVIEW_GAIN = 500
        collection = ee.ImageCollection('L7_L1T_TOA').filterDate(start, end)
        return collection.mosaic().getMapId({
            'bands': ','.join(MAP_IMAGE_BANDS),
            'gain': PREVIEW_GAIN
        })


class NDFI(object):
    """ ndfi info for a period of time
    """

    # hardcoded data for request

    PRODES_IMAGE = {
        "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.ProdesImage',
        "args": ["PRODES_2009"]
    };

    MODIS_BANDS = [
        'sur_refl_b01_250m', 'sur_refl_b02_250m', 'sur_refl_b03_500m',
        'sur_refl_b04_500m', 'sur_refl_b06_500m', 'sur_refl_b07_500m'];

    def __init__(self, ee_res, last_period, work_period):
        self.last_period = dict(start=last_period[0],
                                end=last_period[1])
        self.work_period = dict(start=work_period[0],
                                end=work_period[1])
        self.earth_engine_resource = ee_res
        self.ee = EarthEngine(settings.EE_TOKEN)
        self._image_cache = {}

    def _paint_deforestation(self, asset_id, month, year):
        year_str = "%04d" % (year)
        #end = "%04d%02d" % (year, month)
        return {
          "type": "Image", "creator": "Paint", "args": [asset_id,
          {
            "table_id": int(settings.FT_TABLE_ID), "type": "FeatureCollection",
            "filter":[{"property":"type","equals":7}, {"property":"asset_id","contains":year_str}]},
          4]
        }

    def _mapid2_cmd(self, asset_id, polygon=None, rows=5, cols=5):
        year_msec = 1000 * 60 * 60 * 24 * 365
        month_msec = 1000 * 60 * 60 * 24 * 30
        six_months_ago = self.work_period['end'] - month_msec * 6
        one_month_ago = self.work_period['end'] - month_msec
        last_month = time.gmtime(int(six_months_ago / 1000))[1]
        last_year = time.gmtime(int(six_months_ago / 1000))[0]
        previous_month = time.gmtime(int(one_month_ago / 1000))[1]
        previous_year = time.gmtime(int(one_month_ago / 1000))[0]
        work_month = self._getMidMonth(self.work_period['start'], self.work_period['end'])
        work_year = self._getMidYear(self.work_period['start'], self.work_period['end'])
        end = "%04d%02d" % (work_year, work_month)
        start = "%04d%02d" % (last_year, last_month)
        previous = "%04d%02d" % (previous_year, previous_month)
        start_filter = [{'property':'compounddate','greater_than':start},{'property':'compounddate','less_than':end}]
        deforested_asset = self._paint_deforestation(asset_id, work_month, work_year)
        # 1zqKClXoaHjUovWSydYDfOvwsrLVw-aNU4rh3wLc  was 1868251
        json_cmd = {"creator":CALL_SCOPE + "/com.google.earthengine.examples.sad.GetNDFIDelta","args": [
            self.last_period['start'] - year_msec,
            self.last_period['end'],
            self.work_period['start'],
            self.work_period['end'],
            "MODIS/MOD09GA",
            "MODIS/MOD09GQ",
            {'type':'FeatureCollection','id': 'ft:1zqKClXoaHjUovWSydYDfOvwsrLVw-aNU4rh3wLc', 'mark': str(timestamp()), 'filter':start_filter},
            {'type':'FeatureCollection','id': 'ft:1zqKClXoaHjUovWSydYDfOvwsrLVw-aNU4rh3wLc', 'mark': str(timestamp()),
                'filter':[{"property":"month","equals":work_month},{"property":"year","equals":work_year}]},
            {'type':'FeatureCollection','table_id': 4468280, 'mark': str(timestamp()),
                'filter':[{"property":"Compounddate","equals":int(previous)}]},
            {'type':'FeatureCollection','table_id': 4468280, 'mark': str(timestamp()),
                'filter':[{"property":"Compounddate","equals":int(end)}]},
            deforested_asset,
            polygon,
            rows,
            cols]
        }
        logging.info("GetNDFIDelta")
        logging.info(json_cmd)
        return json_cmd


    def _getMidMonth(self, start, end):
        middle_seconds = int((end + start) / 2000)
        this_time = time.gmtime(middle_seconds)
        return this_time[1]

    def _getMidYear(self, start, end):
        middle_seconds = int((end + start) / 2000)
        this_time = time.gmtime(middle_seconds)
        return this_time[0]

    def mapid2(self, asset_id):
        cmd = {
            "image": json.dumps(self._mapid2_cmd(asset_id)),
            "format": 'png'

        }
        return self._execute_cmd('/mapid', cmd)


    def freeze_map(self, asset_id, table, report_id):
        """
        """
        base_image = {"creator": CALL_SCOPE + "/com.google.earthengine.examples.sad.ProdesImage", "args":[asset_id]};

        remapped = {"algorithm": "Image.remap", "image":base_image,
          "from":[0,1,2,3,4,5,6,7,8,9], "to":[0,1,2,3,4,5,6,2,3,9]}

        def_image = self._paint_call(remapped, int(report_id), table, 7)

        selected_def = {"algorithm": "Image.select", "input": def_image,
                        "bandSelectors":["remapped"]}

        deg_image = self._paint_call(selected_def, int(report_id), table, 8)

        renamed_image = {"algorithm": "Image.select", "input": deg_image,
                        "bandSelectors":["remapped"], "newNames":["classification"]}

        clipped_image = {"creator":CALL_SCOPE + "/com.google.earthengine.examples.sad.AddBB",
                "args":[renamed_image, asset_id, "classification"]}

        map_image = {"algorithm": "Image.addBands", "dstImg": asset_id, "srcImg": clipped_image,
                    "names": ["classification"], "overwrite": True}

        cmd = {"value": json.dumps(map_image)}

        return self._execute_cmd('/create', cmd)

    def _paint_call(self, current_asset, report_id, table, value):
        fc = ee.FeatureCollection(int(table))
        fc = fc.filterMetadata('report_id', 'equals', int(report_id))
        fc = fc.filterMetadata('type', 'equals', value)
        return json.loads(ee.Image(current_asset).paint(fc, value).serialize())

    def rgbid(self):
        """ return params to access NDFI rgb image """
        # get map id from EE
        params = self._RGB_image_command(self.work_period)
        return self._execute_cmd('/mapid', params)

    def smaid(self):
        """ return params to access NDFI rgb image """
        # get map id from EE
        params = self._SMA_image_command(self.work_period)
        return self._execute_cmd('/mapid', params)

    def ndfi0id(self):
        # get map id from EE set long_span=1
        params = self._NDFI_period_image_command(self.last_period, 1)
        return self._execute_cmd('/mapid', params)

    def baseline(self, asset_id):
        params = self._baseline_image_command(asset_id)
        return self._execute_cmd('/mapid', params)

    def rgb0id(self):

        quarter_msec = 1000 * 60 * 60 * 24 * 90
        last_start = self.last_period['start']
        last_period = dict(start=last_start - quarter_msec,
                                 end=self.last_period['end'])
        params = self._RGB_image_command(last_period)
        return self._execute_cmd('/mapid', params)

    def ndfi1id(self):
        # get map id from EE
        params = self._NDFI_period_image_command(self.work_period)
        return self._execute_cmd('/mapid', params)

    def rgb_strech(self, polygon, sensor, bands):
        # this is an special call, the application needs to call /value
        # before call /mapid in order to google earthn engine makes his work
        cmd = self._RGB_streched_command(self.work_period, polygon, sensor, bands)
        del cmd['bands']
        if (sensor=="modis"):
            cmd['fields'] = 'stats_sur_refl_b01,stats_sur_refl_b02,stats_sur_refl_b03,stats_sur_refl_b04,stats_sur_refl_b05'
        else:
            cmd['fields'] = 'stats_30,stats_20,stats_10'

        self._execute_cmd('/value', cmd)
        cmd = self._RGB_streched_command(self.work_period, polygon, sensor, bands)
        return self._execute_cmd('/mapid', cmd)

    def _get_polygon_bbox(self, polygon):
        lats = [x[0] for x in polygon]
        lngs = [x[1] for x in polygon]
        max_lat = max(lats)
        min_lat = min(lats)
        max_lng = max(lngs)
        min_lng = min(lngs)
        return ((min_lat, max_lat), (min_lng, max_lng))

    def _execute_cmd(self, url, cmd):
        params = "&".join(("%s=%s"% v for v in cmd.iteritems()))
        return self.ee.post(url, params)

    def ndfi_change_value(self, asset_id, polygon, rows=5, cols=5):
        img = self._mapid2_cmd(asset_id, polygon, rows, cols)
        cmd = {
            "image": json.dumps(img),
            "fields": 'ndfiSum'#','.join(fields)
        }
        return self._execute_cmd('/value', cmd)


    def _images_for_period(self, period):
        cache_key = "%d-%d" %(period['start'], period['end'])
        if cache_key in self._image_cache:
            img = self._image_cache[cache_key]
        else:
            reference_images = self.ee.get("/list?id=%s&starttime=%s&endtime=%s" % (
                self.earth_engine_resource,
                int(period['start']),
                int(period['end'])
            ))
            logging.info(reference_images)
            img = [x['id'] for x in reference_images['data']]
            self._image_cache[cache_key] = img
        return img

    def _image_composition(self, image_list):
        """ create commands to compose images in google earth engine

            ok, i really have NO idea what's going on :)
        """
        specs = []
        for image in image_list:
            name = image.split("_", 2)[-1]
            specs.append({
              "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.ModisCombiner',
              "args": ['MOD09GA_005_' + name, 'MOD09GQ_005_' + name]
            });
        return specs;

    def _baseline_image(self, asset_id):

        classification = {"algorithm":"Image.select",
                 "input":{"type":"Image", "id":asset_id},
                 "bandSelectors":["classification"]}

        mask = {"algorithm":"Image.eq",
                "image1":classification,
                "image2":{"algorithm":"Constant","value":4}}

        image = {"algorithm":"Image.mask", "image":classification, "mask":mask}
        return image


    def _krig_filter(self, period):
        work_month = self._getMidMonth(period['start'], period['end'])
        work_year = self._getMidYear(period['start'], period['end'])
        end = "%04d%02d" % (work_year, work_month)
        filter = [{'property':'Compounddate','equals':int(end)}]
        return filter



    def _NDFI_image(self, period, long_span=0):
        """ given image list from EE, returns the operator chain to return NDFI image """
        filter = self._krig_filter(period)
        return {
            "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.NDFIImage',
            "args": [{
              "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.UnmixModis',
              "args": [{
                "creator": KRIGING,
                "args": [ self._MakeMosaic(period, long_span),
                        {'type':'FeatureCollection','table_id':4468280,
                                'filter':filter,'mark':str(timestamp())} ]
              }]
            }]
         }

    def _change_detection_data(self, reference_period, work_period, polygons=[], cols=5, rows=5):
        ndfi_image_1 = self._NDFI_image(reference_period)
        ndfi_image_2 = self._NDFI_image(work_period)
        return {
               "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.ChangeDetectionData',
               "args": [ndfi_image_1,
                        ndfi_image_2,
                        self.PRODES_IMAGE,
                        polygons,
                        rows,
                        cols]
        }


    def _NDFI_change_value(self, reference_period, work_period, polygons, cols=5, rows=5):
        """ calc the ndfi change value between two periods inside specified polys

            ``polygons`` are a list of closed polygons defined by lat, lon::

            [
                [ [lat, lon], [lat, lon]...],
                [ [lat, lon], [lat, lon]...]
            ]

        """
        POLY = []
        fields = []

        image = self._change_detection_data(reference_period, work_period, [polygons], cols, rows)
        return {
            "image": json.dumps(image),
            "fields": 'ndfiSum'#','.join(fields)
        }

    def _NDFI_period_image_command(self, period, long_span=0):
        """ get NDFI command to get map of NDFI for a period of time """
        ndfi_image = self._NDFI_image(period, long_span)
        return {
            "image": json.dumps(ndfi_image),
            "bands": 'vis-red,vis-green,vis-blue',
            "gain": 1,
            "bias": 0.0,
            "gamma": 1.6
        }

    def _baseline_image_command(self, asset_id):
        baseline_image = self._baseline_image(asset_id)
        return {
            "image": json.dumps(baseline_image)
        }

    def _RGB_image_command(self, period):
        """ commands for RGB image """
        filter = self._krig_filter(period)
        return {
            "image": json.dumps({
               "creator": KRIGING,
               "args": [ self._MakeMosaic(period),{'type':'FeatureCollection','table_id':4468280,
                        'filter':filter,'mark':str(timestamp())} ]
            }),
            "bands": 'sur_refl_b01,sur_refl_b04,sur_refl_b03',
            "gain": 0.1,
            "bias": 0.0,
            "gamma": 1.6
          };

    def _MakeMosaic(self, period, long_span=0):
        middle_seconds = int((period['end'] + period['start']) / 2000)
        this_time = time.gmtime(middle_seconds)
        month = this_time[1]
        year = this_time[0]
        yesterday = date.today() - timedelta(1)
        micro_yesterday = time.mktime(yesterday.timetuple()) * 1000000
        logging.info("month " + str(month))
        logging.info("year " + str(year))
        if long_span == 0:
          filter = [{'property':'month','equals':month},{'property':'year','equals':year}]
          start_time = period['start']
        else:
          start = "%04d%02d" % (year - 1, month)
          end = "%04d%02d" % (year, month)
          start_time = period['start'] - 1000 * 60 * 60 * 24 * 365
          filter = [{'property':'compounddate','greater_than':start},
                {'or': [{'property':'compounddate','less_than':end}, {'property':'compounddate','equals':end}]}]
        return {
          "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.MakeMosaic',
          "args": [{"id":"MODIS/MOD09GA","version":micro_yesterday,"start_time":start_time,"end_time":period['end']},
                   {"id":"MODIS/MOD09GQ","version":micro_yesterday,"start_time":start_time,"end_time":period['end']},
                   {'type':'FeatureCollection','id':'ft:1zqKClXoaHjUovWSydYDfOvwsrLVw-aNU4rh3wLc',
                      'filter':filter}, start_time, period['end']]
        }

    def _SMA_image_command(self, period):
        filter = self._krig_filter(period)
        return {
            "image": json.dumps({
              "creator": CALL_SCOPE + '/com.google.earthengine.examples.sad.UnmixModis',
              "args": [{
                "creator": KRIGING,
                "args": [self._MakeMosaic(period), {'type':'FeatureCollection','table_id':4468280,'filter':filter}]
              }]
            }),
            "bands": 'gv,soil,npv',
            "gain": 256,
            "bias": 0.0,
            "gamma": 1.6
        };

    def _RGB_streched_command(self, period, polygon, sensor, bands):
     filter = self._krig_filter(period)
     if(sensor=="modis"):
        """ bands in format (1, 2, 3) """
        bands = "sur_refl_b0%d,sur_refl_b0%d,sur_refl_b0%d" % bands
        return {
            "image": json.dumps({
                "creator":CALL_SCOPE + "/com.google.earthengine.examples.sad.StretchImage",
                "args":[{
                    "creator":"ClipToMultiPolygon",
                    "args":[
                    {
                        "creator":KRIGING,
                        "args":[ self._MakeMosaic(period), {'type':'FeatureCollection','table_id':4468280,
                                'filter':filter}]
                    },
                    polygon]},
                 ["sur_refl_b01","sur_refl_b02","sur_refl_b03","sur_refl_b04","sur_refl_b05"],
                 2
                 ]
            }),
            "bands": bands
        }
     else:
        three_months = timedelta(days=90)
        work_period_end   = self.work_period['end']
        work_period_start = self.work_period['start'] - 7776000000 #three_months
        yesterday = date.today() - timedelta(1)
        micro_yesterday = time.mktime(yesterday.timetuple()) * 1000000
        landsat_bands = ['10','20','30','40','50','70','80','61','62']
        creator_bands =[{'id':id, 'data_type':'float'} for id in landsat_bands]
        bands = "%d,%d,%d" % bands
        return {
            "image": json.dumps({
                "creator":CALL_SCOPE + "/com.google.earthengine.examples.sad.StretchImage",
                "args":[{
                    "creator":"LonLatReproject",
                    "args":[{
                       "creator":"SimpleMosaic",
                       "args":[{
                          "creator":"LANDSAT/LandsatTOA",
                          "input":{"id":"LANDSAT/L7_L1T","version":micro_yesterday},
                          "bands":creator_bands,
                          "start_time": work_period_start, #131302801000
                          "end_time": work_period_end }] #1313279999000
                    },polygon, 30]
                 },
                 landsat_bands,
                 2
                 ]
            }),
            "bands": bands
        }


def get_prodes_stats(assetids, table_id):
    results = []
    for assetid in assetids:
        prodes_image, classes = _remap_prodes_classes(ee.Image(assetid))
        collection = ee.FeatureCollection(table_id)
        raw_stats = _get_area_histogram(prodes_image, collection, classes)
        stats = {}
        for raw_stat in raw_stats:
            values = {}
            for class_value in range(max(classes) + 1):
                class_value = str(class_value)
                if class_value in raw_stat:
                    values[class_value] = raw_stat[class_value]['area']
                else:
                    values[class_value] = 0.0
            stats[str(int(raw_stat['name']))] = {
                'values': values,
                'type': 'DataDictionary'
            }
        results.append({'values': stats, 'type': 'DataDictionary'})
    return {'data': {'properties': {'classHistogram': results}}}


def get_thumbnail(landsat_image_id):
    return ee.data.getThumbId({
        'image': ee.Image(landsat_image_id).serialize(),
        'bands': '30,20,10'
    })


def _get_area_histogram(image, polygons, classes, scale=120):
    area = ee.Image({'algorithm': 'Image.area'})
    sum_reducer = ee.call('Reducer.sum')

    def calculateArea(feature):
        geometry = feature.geometry()
        total = area.mask(image.mask())
        total_area = total.reduceRegion(
            geometry, sum_reducer, scale, bestEffort=True)
        properties = {'total': total_area}

        for class_value in classes:
            masked = area.mask(image.eq(class_value))
            class_area = masked.reduceRegion(
                geometry, sum_reducer, scale, bestEffort=True)
            properties[str(class_value)] = class_area

        return ee.call('SetProperties', feature, properties)

    result = polygons.map(calculateArea).getInfo()
    return [i['properties'] for i in result['features']]


def _make_prodes_image(prodes):
    """Create a baseline classification image from an ingested PRODES map."""
    remapped = _remap_prodes_classes(prodes)
    return ee.Image.cat(remapped, _visualize_classes(remapped), prodes)


def _visualize_classes(img):
    PALETTE = [
        'ffffff',  # unclassified
        '00ff00',  # forest  (was 0x00c800)
        'ee0000',  # deforested (was 0xff0000)
        'ff7150',  # degraded
        '000000',  # baseline
        'd8bfd8',  # cloud (was 0x7f7f7f)
        '0000ff',  # old_deforestation (was ffff00)
        '00ffff',  # edited deforestation
        'a020f0',  # edited degradation
        'ff00ff'   # edited old deforestation
    ]
    return img.visualize(
        null, null, null, [0], [PALETTE.length - 1], null, null, PALETTE)


def _remap_prodes_classes(img):
    RE_FOREST = re.compile(r'^floresta$')
    RE_BASELINE = re.compile(r'^(baseline|d[12]\d{3}.*)$')
    RE_DEFORESTATION = re.compile(r'^desmatamento$')
    RE_DEGRADATION = re.compile(r'^degradacao$')
    RE_CLOUD = re.compile(r'^nuvem$')
    RE_NEW_DEFORESTATION = re.compile(r'^new_deforestation$');
    RE_OLD_DEFORESTATION = re.compile(r'^desmat antigo$');
    RE_EDITED_DEFORESTATION = re.compile(r'^desmat editado$');
    RE_EDITED_DEGRADATION = re.compile(r'^degrad editado$');
    RE_EDITED_OLD_DEGRADATION = re.compile(r'^desmat antigo editado$');

    UNCLASSIFIED = 0
    FOREST = 1
    DEFORESTED = 2
    DEGRADED = 3
    BASELINE = 4
    CLOUD = 5
    OLD_DEFORESTATION = 6
    EDITED_DEFORESTATION = 7
    EDITED_DEGRADATION = 8
    EDITED_OLD_DEGRADATION = 9

    metadata = img.getInfo()['bands'][0]['properties']
    class_names = metadata['class_names']
    classes_from = metadata['class_indexes']
    classes_to = []

    for src_class, name in zip(classes_from, class_names):
      dst_class = UNCLASSIFIED

      if RE_FOREST.match(name):
        dst_class = FOREST
      elif RE_BASELINE.match(name):
        dst_class = BASELINE
      elif RE_CLOUD.match(name):
        dst_class = CLOUD
      elif RE_NEW_DEFORESTATION.match(name):
        dst_class = DEFORESTED
      elif RE_DEFORESTATION.match(name):
        dst_class = DEFORESTED
      elif RE_DEGRADATION.match(name):
        dst_class = DEGRADED
      elif RE_OLD_DEFORESTATION.match(name):
        dst_class = OLD_DEFORESTATION
      elif RE_EDITED_DEFORESTATION.match(name):
        dst_class = EDITED_DEFORESTATION
      elif RE_EDITED_DEGRADATION.match(name):
        dst_class = EDITED_DEGRADATION
      elif RE_EDITED_OLD_DEGRADATION.match(name):
        dst_class = EDITED_OLD_DEGRADATION

      classes_to.append(dst_class)

    remapped = img.remap(classes_from, classes_to, UNCLASSIFIED)
    final = remapped.mask(img.mask()).select(['remapped'], ['class'])
    return (final, set(classes_to))
