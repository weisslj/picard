# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2004 Robert Kaye
# Copyright (C) 2006 Lukáš Lalinský
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import re
from heapq import heappush, heappop
from PyQt4 import QtCore
from picard.metadata import Metadata
from picard.similarity import similarity2, similarity
from picard.ui.item import Item
from picard.util import format_time
from picard.mbxml import artist_credit_from_node


class Cluster(QtCore.QObject, Item):

    # Weights for different elements when comparing a cluster to a release
    comparison_weights = {
        'album': 17,
        'artist': 6,
        'totaltracks': 5,
        'releasecountry': 2,
        'format': 2,
    }

    def __init__(self, name, artist="", special=False, related_album=None, hide_if_empty=False):
        QtCore.QObject.__init__(self)
        self.item = None
        self.metadata = Metadata()
        self.metadata['album'] = name
        self.metadata['albumartist'] = artist
        self.metadata['totaltracks'] = 0
        self.special = special
        self.hide_if_empty = hide_if_empty
        self.related_album = related_album
        self.files = []
        self.lookup_task = None

    def __repr__(self):
        return '<Cluster %r>' % self.metadata['album']

    def __len__(self):
        return len(self.files)

    def add_files(self, files):
        self.metadata['totaltracks'] += len(files)
        for file in files:
            self.metadata.length += file.metadata.length
            file._move(self)
            file.update(signal=False)
        self.files.extend(files)
        self.item.add_files(files)

    def add_file(self, file):
        self.metadata['totaltracks'] += 1
        self.metadata.length += file.metadata.length
        self.files.append(file)
        file.update(signal=False)
        self.item.add_file(file)

    def remove_file(self, file):
        self.metadata['totaltracks'] -= 1
        self.metadata.length -= file.metadata.length
        self.files.remove(file)
        self.item.remove_file(file)
        if not self.special and self.get_num_files() == 0:
            self.tagger.remove_cluster(self)

    def update(self):
        if self.item:
            self.item.update()

    def get_num_files(self):
        return len(self.files)

    def iterfiles(self, save=False):
        for file in self.files:
            yield file

    def can_save(self):
        """Return if this object can be saved."""
        if self.files:
            return True
        else:
            return False

    def can_remove(self):
        """Return if this object can be removed."""
        return not self.special

    def can_edit_tags(self):
        """Return if this object supports tag editing."""
        return True

    def can_analyze(self):
        """Return if this object can be fingerprinted."""
        return any([_file.can_analyze() for _file in self.files])

    def can_autotag(self):
        return True

    def can_refresh(self):
        return False

    def can_browser_lookup(self):
        return not self.special

    def is_album_like(self):
        return True

    def column(self, column):
        if column == 'title':
            return '%s (%d)' % (self.metadata['album'], self.metadata['totaltracks'])
        elif (column == '~length' and self.special) or column == 'album':
            return ''
        elif column == '~length':
            return format_time(self.metadata.length)
        elif column == 'artist':
            return self.metadata['albumartist']
        return self.metadata[column]

    def _compare_to_release(self, release):
        """
        Compare cluster metadata to a MusicBrainz release. Produces a
        probability as a linear combination of weights that the
        cluster is a certain album.

        Weights:
          * title                = 17
          * artist name          = 6
          * number of tracks     = 5
          * release country      = 2
          * format               = 2

        """
        total = 0.0
        parts = []
        w = Cluster.comparison_weights

        a = self.metadata['albumartist']
        b = artist_credit_from_node(release.artist_credit[0], self.config)[0]
        parts.append((similarity2(a, b), w["artist"]))
        total += w["artist"]

        t, p = self.metadata.compare_to_release(release, w, self.config)
        total += t
        parts.extend(p)

        return reduce(lambda x, y: x + y[0] * y[1] / total, parts, 0.0)

    def _lookup_finished(self, document, http, error):
        self.lookup_task = None

        try:
            releases = document.metadata[0].release_list[0].release
        except (AttributeError, IndexError):
            releases = None

        # no matches
        if not releases:
            self.tagger.window.set_statusbar_message(N_("No matching releases for cluster %s"), self.metadata['album'], timeout=3000)
            return

        # multiple matches -- calculate similarities to each of them
        matches = []
        for release in releases:
            matches.append((self._compare_to_release(release), release))
        matches.sort(reverse=True)
        #self.log.debug("Matches: %r", matches)

        if matches[0][0] < self.config.setting['cluster_lookup_threshold']:
            self.tagger.window.set_statusbar_message(N_("No matching releases for cluster %s"), self.metadata['album'], timeout=3000)
            return
        self.tagger.window.set_statusbar_message(N_("Cluster %s identified!"), self.metadata['album'], timeout=3000)
        self.tagger.move_files_to_album(self.files, matches[0][1].id)

    def lookup_metadata(self):
        """ Try to identify the cluster using the existing metadata. """
        self.tagger.window.set_statusbar_message(N_("Looking up the metadata for cluster %s..."), self.metadata['album'])
        self.lookup_task = self.tagger.xmlws.find_releases(self._lookup_finished,
            artist=self.metadata['albumartist'],
            release=self.metadata['album'],
            tracks=str(len(self.files)),
            limit=25)

    def clear_lookup_task(self):
        if self.lookup_task:
            self.tagger.xmlws.remove_task(self.lookup_task)
            self.lookup_task = None

    @staticmethod
    def cluster(files, threshold):
        artistDict = ClusterDict()
        albumDict = ClusterDict()
        tracks = []
        for file in files:
            album = file.metadata["album"]
            # For each track, record the index of the artist and album within the clusters
            tracks.append((artistDict.add(file.metadata["artist"]),
                           albumDict.add(album)))

        artist_cluster_engine = ClusterEngine(artistDict)
        artist_cluster = artist_cluster_engine.cluster(threshold)

        album_cluster_engine = ClusterEngine(albumDict)
        album_cluster = album_cluster_engine.cluster(threshold)

        # Arrange tracks into albums
        albums = {}
        for i in xrange(len(tracks)):
            cluster = album_cluster_engine.getClusterFromId(tracks[i][1])
            if cluster is not None:
                albums.setdefault(cluster, []).append(i)

        # Now determine the most prominent names in the cluster and build the
        # final cluster list
        for album_id, album in albums.items():
            album_name = album_cluster_engine.getClusterTitle(album_id)

            artist_max = 0
            artist_id = None
            artist_hist = {}
            for track_id in album:
                cluster = artist_cluster_engine.getClusterFromId(
                     tracks[track_id][0])
                cnt = artist_hist.get(cluster, 0) + 1
                if cnt > artist_max:
                    artist_max = cnt
                    artist_id = cluster
                artist_hist[cluster] = cnt

            if artist_id is None:
                artist_name = u"Various Artists"
            else:
                artist_name = artist_cluster_engine.getClusterTitle(artist_id)

            yield album_name, artist_name, (files[i] for i in album)


class UnmatchedFiles(Cluster):
    """Special cluster for 'Unmatched Files' which have no PUID and have not been clustered."""

    def __init__(self):
        super(UnmatchedFiles, self).__init__(_(u"Unmatched Files"), special=True)

    def add_files(self, files):
        Cluster.add_files(self, files)
        self.tagger.window.enable_cluster(self.get_num_files() > 0)

    def add_file(self, file):
        Cluster.add_file(self, file)
        self.tagger.window.enable_cluster(self.get_num_files() > 0)

    def remove_file(self, file):
        Cluster.remove_file(self, file)
        self.tagger.window.enable_cluster(self.get_num_files() > 0)

    def lookup_metadata(self):
        self.tagger.autotag(self.files)

    def can_edit_tags(self):
        return False

    def can_autotag(self):
        return len(self.files) > 0


class ClusterList(list, Item):
    """A list of clusters."""

    def __init__(self):
        super(ClusterList, self).__init__()

    def __hash__(self):
        return id(self)

    def iterfiles(self, save=False):
        for cluster in self:
            for file in cluster.iterfiles(save):
                yield file

    def can_save(self):
        return len(self) > 0

    def can_analyze(self):
        return any([cluster.can_analyze() for cluster in self])

    def can_autotag(self):
        return len(self) > 0

    def can_browser_lookup(self):
        return False


class ClusterDict(object):

    def __init__(self):
        # word -> id index
        self.words = {}
        # id -> word, token index
        self.ids = {}
        # counter for new id generation
        self.id = 0
        self.regexp = re.compile(ur'\W', re.UNICODE)
        self.spaces = re.compile(ur'\s', re.UNICODE)

    def getSize(self):
        return self.id

    def tokenize(self, word):
        token = self.regexp.sub(u'', word.lower())
        return token if token else self.spaces.sub(u'', word.lower())

    def add(self, word):
        """
        Add a new entry to the cluster if it does not exist. If it
        does exist, increment the count. Return the index of the word
        in the dictionary or -1 is the word is empty.
        """

        if word == u'':
           return -1

        token = self.tokenize(word)
        if token == u'':
           return -1

        try:
           index, count = self.words[word]
           self.words[word] = (index, count + 1)
        except KeyError:
           index = self.id
           self.words[word] = (self.id, 1)
           self.ids[index] = (word, token)
           self.id = self.id + 1

        return index

    def getWord(self, index):
        word = None
        try:
            word, token = self.ids[index]
        except KeyError:
            pass
        return word

    def getToken(self, index):
        token = None;
        try:
            word, token = self.ids[index]
        except KeyError:
            pass
        return token

    def getWordAndCount(self, index):
        word = None
        count = 0
        try:
           word, token = self.ids[index]
           index, count = self.words[word]
        except KeyError:
           pass
        return word, count


class ClusterEngine(object):

    def __init__(self, clusterDict):
        # the cluster dictionary we're using
        self.clusterDict = clusterDict
        # keeps track of unique cluster index
        self.clusterCount = 0
        # Keeps track of the clusters we've created
        self.clusterBins = {}
        # Index the word ids -> clusters
        self.idClusterIndex = {}

    def getClusterFromId(self, id):
        return self.idClusterIndex.get(id)

    def printCluster(self, cluster):
        if cluster < 0:
            print "[no such cluster]"
            return

        bin = self.clusterBins[cluster]
        print cluster, " -> ", ", ".join([("'" + self.clusterDict.getWord(i) + "'") for i in bin])

    def getClusterTitle(self, cluster):

        if cluster < 0:
            return ""

        max = 0
        maxWord = u''
        for id in self.clusterBins[cluster]:
            word, count = self.clusterDict.getWordAndCount(id)
            if count >= max:
                maxWord = word
                max = count

        return maxWord

    def cluster(self, threshold):

        # Keep the matches sorted in a heap
        heap = []

        for y in xrange(self.clusterDict.getSize()):
            for x in xrange(y):
                if x != y:
                    c = similarity(self.clusterDict.getToken(x).lower(),
                                   self.clusterDict.getToken(y).lower())
                    #print "'%s' - '%s' = %f" % (
                    #    self.clusterDict.getToken(x).encode('utf-8', 'replace').lower(),
                    #    self.clusterDict.getToken(y).encode('utf-8', 'replace').lower(), c)

                    if c >= threshold:
                        heappush(heap, ((1.0 - c), [x, y]))
            QtCore.QCoreApplication.processEvents()

        for i in xrange(self.clusterDict.getSize()):
            word, count = self.clusterDict.getWordAndCount(i)
            if word and count > 1:
                self.clusterBins[self.clusterCount] = [ i ]
                self.idClusterIndex[i] = self.clusterCount
                self.clusterCount = self.clusterCount + 1
                #print "init ",
                #self.printCluster(self.clusterCount - 1)

        for i in xrange(len(heap)):
            c, pair = heappop(heap)
            c = 1.0 - c

            try:
                match0 = self.idClusterIndex[pair[0]]
            except:
                match0 = -1

            try:
                match1 = self.idClusterIndex[pair[1]]
            except:
                match1 = -1

            # if neither item is in a cluster, make a new cluster
            if match0 == -1 and match1 == -1:
                self.clusterBins[self.clusterCount] = [pair[0], pair[1]]
                self.idClusterIndex[pair[0]] = self.clusterCount
                self.idClusterIndex[pair[1]] = self.clusterCount
                self.clusterCount = self.clusterCount + 1
                #print "new ",
                #self.printCluster(self.clusterCount - 1)
                continue

            # If cluster0 is in a bin, stick the other match into that bin
            if match0 >= 0 and match1 < 0:
                self.clusterBins[match0].append(pair[1])
                self.idClusterIndex[pair[1]] = match0
                #print "add '%s' to cluster " % (self.clusterDict.getWord(pair[0])),
                #self.printCluster(match0)
                continue

            # If cluster1 is in a bin, stick the other match into that bin
            if match1 >= 0 and match0 < 0:
                self.clusterBins[match1].append(pair[0])
                self.idClusterIndex[pair[0]] = match1
                #print "add '%s' to cluster " % (self.clusterDict.getWord(pair[1])),
                #self.printCluster(match0)
                continue

            # If both matches are already in two different clusters, merge the clusters
            if match1 != match0:
                self.clusterBins[match0].extend(self.clusterBins[match1])
                for match in self.clusterBins[match1]:
                    self.idClusterIndex[match] = match0
                #print "col cluster %d into cluster" % (match1),
                #self.printCluster(match0)
                del self.clusterBins[match1]

        return self.clusterBins

    def can_refresh(self):
        return False

