# pyright: reportUnknownArgumentType=none,reportUnknownMemberType=none,reportUnknownVariableType=none,reportTypedDictNotRequiredAccess=none,reportOptionalMemberAccess=none,reportMissingTypeStubs=none

from .config import config
from .discord import DiscordIpcService
from config.constants import name, plexClientID
from plexapi.alert import AlertListener
from plexapi.media import Genre, Guid
from plexapi.myplex import MyPlexAccount, PlexServer
from typing import Optional
from utils.cache import getCacheKey, setCacheKey
from utils.logging import LoggerWithPrefix
from utils.text import formatSeconds, truncate, stripNonAscii
import models.config
import models.discord
import models.plex
import requests
import threading
import time
import urllib.parse
from .tmdb import get_tmdb_poster_url

def initiateAuth() -> tuple[str, str, str]:
	response = requests.post("https://plex.tv/api/v2/pins.json?strong=true", headers = {
		"X-Plex-Product": name,
		"X-Plex-Client-Identifier": plexClientID,
	}).json()
	authUrl = f"https://app.plex.tv/auth#?clientID={plexClientID}&code={response['code']}&context%%5Bdevice%%5D%%5Bproduct%%5D={urllib.parse.quote(name)}"
	return response["id"], response["code"], authUrl

def getAuthToken(id: str, code: str) -> Optional[str]:
	response = requests.get(f"https://plex.tv/api/v2/pins/{id}.json?code={code}", headers = {
		"X-Plex-Client-Identifier": plexClientID,
	}).json()
	return response["authToken"]

mediaTypeActivityTypeMap = {
	"movie": models.discord.ActivityType.WATCHING,
	"episode": models.discord.ActivityType.WATCHING,
	"live_episode": models.discord.ActivityType.WATCHING,
	"track": models.discord.ActivityType.LISTENING,
	"clip": models.discord.ActivityType.WATCHING,
}
buttonTypeGuidTypeMap = {
	"imdb": "imdb",
	"tmdb": "tmdb",
	"thetvdb": "tvdb",
	"trakt": "tmdb",
	"letterboxd": "tmdb",
	"musicbrainz": "mbid",
}

class PlexAlertListener(threading.Thread):

	productName = "Plex Media Server"
	updateTimeoutTimerInterval = 30
	connectionCheckTimerInterval = 60
	disconnectTimerInterval = 3
	maximumIgnores = 2

	def __init__(self, token: str, serverConfig: models.config.Server):
		super().__init__()
		self.daemon = True
		self.token = token
		self.serverConfig = serverConfig
		self.logger = LoggerWithPrefix(f"[{self.serverConfig['name']}] ")
		self.discordIpcService = DiscordIpcService(self.serverConfig.get("ipcPipeNumber"))
		self.updateTimeoutTimer: Optional[threading.Timer] = None
		self.connectionCheckTimer: Optional[threading.Timer] = None
		self.disconnectTimer: Optional[threading.Timer] = None
		self.account: Optional[MyPlexAccount] = None
		self.server: Optional[PlexServer] = None
		self.alertListener: Optional[AlertListener] = None
		self.lastState, self.lastSessionKey, self.lastRatingKey = "", 0, 0
		self.listenForUser, self.isServerOwner, self.ignoreCount = "", False, 0
		self.start()

	def run(self) -> None:
		while True:
			try:
				self.logger.info("Signing into Plex")
				self.account = MyPlexAccount(token = self.token)
				self.logger.info("Signed in as Plex user '%s'", self.account.username)
				self.listenForUser = self.serverConfig.get("listenForUser", "") or self.account.username
				self.server = None
				for resource in self.account.resources():
					if resource.product == self.productName and resource.name.lower() == self.serverConfig["name"].lower():
						self.logger.info("Connecting to %s '%s'", self.productName, self.serverConfig["name"])
						self.server = resource.connect()
						try:
							self.server.myPlexAccount()
							self.isServerOwner = True
						except:
							pass
						self.logger.info("Connected to %s '%s'", self.productName, resource.name)
						self.alertListener = AlertListener(self.server, self.tryHandleAlert, self.reconnect)
						self.alertListener.start()
						self.logger.info("Listening for alerts from user '%s'", self.listenForUser)
						self.connectionCheckTimer = threading.Timer(self.connectionCheckTimerInterval, self.connectionCheck)
						self.connectionCheckTimer.start()
						return
				if not self.server:
					raise Exception("Server not found")
			except Exception as e:
				self.logger.error("Failed to connect to %s '%s': %s", self.productName, self.serverConfig["name"], e)
				self.logger.error("Reconnecting in 10 seconds")
				time.sleep(10)

	def disconnect(self) -> None:
		if self.alertListener:
			try:
				self.alertListener.stop()
			except:
				pass
		self.disconnectRpc()
		if self.connectionCheckTimer:
			self.connectionCheckTimer.cancel()
			self.connectionCheckTimer = None
		self.account, self.server, self.alertListener, self.listenForUser, self.isServerOwner, self.ignoreCount = None, None, None, "", False, 0
		self.logger.info("Stopped listening for alerts")

	def reconnect(self, exception: Exception) -> None:
		self.logger.error("Connection to Plex lost: %s", exception)
		self.disconnect()
		self.logger.error("Reconnecting")
		self.run()

	def disconnectRpc(self) -> None:
		self.lastState, self.lastSessionKey, self.lastRatingKey = "", 0, 0
		if self.discordIpcService.connected:
			self.discordIpcService.disconnect()
		if self.updateTimeoutTimer:
			self.updateTimeoutTimer.cancel()
			self.updateTimeoutTimer = None

	def updateTimeout(self) -> None:
		self.logger.debug("No recent updates from session key %s", self.lastSessionKey)
		self.disconnectRpc()

	def connectionCheck(self) -> None:
		try:
			self.logger.debug("Running periodic connection check")
			self.server.clients()
		except Exception as e:
			self.reconnect(e)
		else:
			self.connectionCheckTimer = threading.Timer(self.connectionCheckTimerInterval, self.connectionCheck)
			self.connectionCheckTimer.start()

	def tryHandleAlert(self, alert: models.plex.Alert) -> None:
		try:
			self.handleAlert(alert)
		except:
			self.logger.exception("An unexpected error occured in the Plex alert handler")
			self.disconnectRpc()

	def handleAlert(self, alert: models.plex.Alert) -> None:
		if alert["type"] != "playing" or "PlaySessionStateNotification" not in alert:
			return
		stateNotification = alert["PlaySessionStateNotification"][0]
		self.logger.debug("Received alert: %s", stateNotification)
		ratingKey = int(stateNotification["ratingKey"])
		item = self.server.fetchItem(ratingKey)
		if item.key and item.key.startswith("/livetv"):
			mediaType = "live_episode"
		else:
			mediaType = item.type
		if mediaType not in mediaTypeActivityTypeMap:
			self.logger.debug("Unsupported media type '%s', ignoring", mediaType)
			return
		state = stateNotification["state"]
		sessionKey = int(stateNotification["sessionKey"])
		viewOffset = int(stateNotification["viewOffset"])
		try:
			libraryName = item.section().title
		except:
			libraryName = "ERROR"
		if "blacklistedLibraries" in self.serverConfig and libraryName in self.serverConfig["blacklistedLibraries"]:
			self.logger.debug("Library '%s' is blacklisted, ignoring", libraryName)
			return
		if "whitelistedLibraries" in self.serverConfig and libraryName not in self.serverConfig["whitelistedLibraries"]:
			self.logger.debug("Library '%s' is not whitelisted, ignoring", libraryName)
			return
		isIgnorableState = state == "stopped" or (state == "paused" and not config["display"]["paused"])
		if self.lastSessionKey == sessionKey and self.lastRatingKey == ratingKey:
			if self.updateTimeoutTimer:
				self.updateTimeoutTimer.cancel()
				self.updateTimeoutTimer = None
			if self.lastState == state and self.ignoreCount < self.maximumIgnores:
				self.logger.debug("Nothing changed, ignoring")
				self.ignoreCount += 1
				self.updateTimeoutTimer = threading.Timer(self.updateTimeoutTimerInterval, self.updateTimeout)
				self.updateTimeoutTimer.start()
				return
			else:
				self.ignoreCount = 0
				if isIgnorableState:
					if self.disconnectTimer:
						self.disconnectTimer.cancel()
					self.disconnectTimer = threading.Timer(self.disconnectTimerInterval, self.disconnectRpc)
					self.disconnectTimer.start()
					return
		elif isIgnorableState:
			self.logger.debug("Received '%s' state alert from unknown session, ignoring", state)
			return
		if self.isServerOwner:
			self.logger.debug("Searching sessions for session key %s", sessionKey)
			sessions = self.server.sessions()
			if len(sessions) < 1:
				self.logger.debug("Empty session list, ignoring")
				return
			for session in sessions:
				self.logger.debug("%s, Session Key: %s, Usernames: %s", session, session.sessionKey, session.usernames)
				if session.sessionKey == sessionKey:
					self.logger.debug("Session found")
					sessionUsername = session.usernames[0]
					if sessionUsername.lower() == self.listenForUser.lower():
						self.logger.debug("Username '%s' matches '%s', continuing", sessionUsername, self.listenForUser)
						break
					self.logger.debug("Username '%s' doesn't match '%s', ignoring", sessionUsername, self.listenForUser)
					return
			else:
				self.logger.debug("No matching session found, ignoring")
				return
		if self.updateTimeoutTimer:
			self.updateTimeoutTimer.cancel()
		self.updateTimeoutTimer = threading.Timer(self.updateTimeoutTimerInterval, self.updateTimeout)
		self.updateTimeoutTimer.start()
		if self.disconnectTimer:
			self.disconnectTimer.cancel()
			self.disconnectTimer = None
		self.lastState, self.lastSessionKey, self.lastRatingKey = state, sessionKey, ratingKey
		stateStrings: list[str] = []
		if config["display"]["duration"] and item.duration and mediaType != "track":
			stateStrings.append(formatSeconds(item.duration / 1000))
		largeText, thumb, smallText, smallThumb = "", "", "", ""
		if mediaType == "movie":
			tmdb_title = item.title.split(' (')[0] if item.title else ""
			title = shortTitle = item.title
			if config["display"]["year"] and item.year:
				stateStrings.append(str(item.year))
			if config["display"]["genres"] and item.genres:
				genres: list[Genre] = item.genres[:3]
				stateStrings.append(f"{', '.join(genre.tag for genre in genres)}")
			thumb = item.thumb
			self.logger.info(f"Calling get_tmdb_poster_url for movie: title={tmdb_title or item.title or ''}, year={getattr(item, 'year', None)}, guids={[g.id for g in item.guids] if hasattr(item, 'guids') else []}")
		elif mediaType == "episode":
			tmdb_title = item.grandparentTitle.split(' (')[0] if item.grandparentTitle else ""
			title = shortTitle = item.grandparentTitle
			if config["display"]["year"]:
				grandparent = self.server.fetchItem(item.grandparentRatingKey)
				if grandparent.year:
					stateStrings.append(str(grandparent.year))
			stateStrings.append(f"S{item.parentIndex:02}E{item.index:02}")
			stateStrings.append(item.title)
			thumb = item.grandparentThumb
			self.logger.info(f"Calling get_tmdb_poster_url for episode: title={tmdb_title or item.grandparentTitle or ''}, year={getattr(item, 'year', None)}, guids={[g.id for g in item.guids] if hasattr(item, 'guids') else []}")
		elif mediaType == "live_episode":
			title = shortTitle = item.grandparentTitle
			if item.title != item.grandparentTitle:
				stateStrings.append(item.title)
			thumb = item.grandparentThumb
		elif mediaType == "track":
			title = shortTitle = item.title
			if config["display"]["album"]:
				largeText = item.parentTitle
				if config["display"]["year"]:
					parent = self.server.fetchItem(item.parentRatingKey)
					if parent.year:
						largeText = f"{truncate(largeText, 110)} ({parent.year})"
			if config["display"]["albumImage"]:
				thumb = item.thumb
			if config["display"]["artist"]:
				stateStrings.append(item.originalTitle or item.grandparentTitle)
			if config["display"]["artistImage"]:
				smallText = item.grandparentTitle or item.originalTitle
				smallThumb = item.grandparentThumb
		else:
			title = shortTitle = item.title
			thumb = item.thumb
		if state != "playing" and mediaType != "track":
			if config["display"]["progressMode"] == "remaining":
				stateStrings.append(f"{formatSeconds((item.duration - viewOffset) / 1000, ':')} left")
			else:
				stateStrings.append(f"{formatSeconds(viewOffset / 1000, ':')} elapsed")
			if not config["display"]["statusIcon"]:
				stateStrings.append(state.capitalize())
		stateText = " · ".join(stateString for stateString in stateStrings if stateString)
		thumbUrl = ""
		smallThumbUrl = ""
		if config["display"]["posters"]["enabled"]:
			if mediaType == "movie":
				guids = [g.id for g in item.guids] if hasattr(item, 'guids') else []
				self.logger.info(f"Calling get_tmdb_poster_url for movie: title={tmdb_title or item.title or ''}, year={getattr(item, 'year', None)}, guids={guids}")
				thumbUrl = get_tmdb_poster_url(
					guids=guids,
					title=tmdb_title or item.title or "",
					year=getattr(item, 'year', None),
					media_type="movie"
				) or ""
			elif mediaType == "episode":
				grandparent = self.server.fetchItem(item.grandparentRatingKey)
				guids = [g.id for g in grandparent.guids] if hasattr(grandparent, 'guids') else []
				self.logger.info(f"Calling get_tmdb_poster_url for episode: title={tmdb_title or grandparent.title or ''}, year={getattr(grandparent, 'year', None)}, guids={guids}")
				thumbUrl = get_tmdb_poster_url(
					guids=guids,
					title=tmdb_title or grandparent.title or "",
					year=getattr(grandparent, 'year', None),
					media_type="tv"
				) or ""
			# For music or other types, fallback to original thumb (no TMDB)
			elif thumb:
				thumbUrl = self.server.url(thumb, True)
			if smallThumb:
				smallThumbUrl = self.server.url(smallThumb, True)
		activity: models.discord.Activity = {
			"type": mediaTypeActivityTypeMap[mediaType],
			"details": truncate(title, 120),
		}
		if config["display"]["statusIcon"]:
			smallText = smallText or state.capitalize()
			smallThumbUrl = smallThumbUrl or state
		if largeText or thumbUrl or smallText or smallThumbUrl:
			activity["assets"] = {}
			if largeText:
				activity["assets"]["large_text"] = truncate(largeText, 120)
			if thumbUrl:
				activity["assets"]["large_image"] = thumbUrl
			if smallText:
				activity["assets"]["small_text"] = truncate(smallText, 120)
			if smallThumbUrl:
				activity["assets"]["small_image"] = smallThumbUrl
		if stateText:
			activity["state"] = truncate(stateText, 120)
		if config["display"]["buttons"]:
			guidsRaw: list[Guid] = []
			if mediaType in ["movie", "track"]:
				guidsRaw = item.guids
			elif mediaType == "episode":
				guidsRaw = self.server.fetchItem(item.grandparentRatingKey).guids
			guids: list[str] = [guid.id for guid in guidsRaw]
			buttons: list[models.discord.ActivityButton] = []
			for button in config["display"]["buttons"]:
				if "mediaTypes" in button and mediaType not in button["mediaTypes"]:
					continue
				label = truncate(button["label"].format(title = stripNonAscii(shortTitle)), 30)
				if not button["url"].startswith("dynamic:"):
					buttons.append({ "label": label, "url": button["url"] })
					continue
				buttonType = button["url"][8:]
				guidType = buttonTypeGuidTypeMap.get(buttonType)
				if not guidType:
					continue
				guid = next((guid for guid in guids if guid.startswith(f"{guidType}://")), None)
				if not guid:
					continue
				url = ""
				if buttonType == "imdb":
					url = f"https://www.imdb.com/title/{guid.split('://')[1]}"
				elif buttonType == "tmdb":
					tmdbPathSegment = "movie" if mediaType == "movie" else "tv"
					url = f"https://www.themoviedb.org/{tmdbPathSegment}/{guid.split('://')[1]}"
				elif buttonType == "thetvdb":
					theTvdbPathSegment = "movie" if mediaType == "movie" else "series"
					url = f"https://www.thetvdb.com/dereferrer/{theTvdbPathSegment}/{guid.split('://')[1]}"
				elif buttonType == "trakt":
					idType = "movie" if mediaType == "movie" else "show"
					url = f"https://trakt.tv/search/tmdb/{guid.split('://')[1]}?id_type={idType}"
				elif buttonType == "letterboxd" and mediaType == "movie":
					url = f"https://letterboxd.com/tmdb/{guid.split('://')[1]}"
				elif buttonType == "musicbrainz":
					url = f"https://musicbrainz.org/track/{guid.split('://')[1]}"
				if url:
					buttons.append({ "label": label, "url": url })
			if buttons:
				activity["buttons"] = buttons[:2]
		if state == "playing":
			currentTimestamp = int(time.time() * 1000)
			match config["display"]["progressMode"]:
				case "elapsed":
					activity["timestamps"] = { "start": round(currentTimestamp - viewOffset) }
				case "remaining":
					activity["timestamps"] = { "end": round(currentTimestamp + (item.duration - viewOffset)) }
				case "bar":
					activity["timestamps"] = { "start": round(currentTimestamp - viewOffset), "end": round(currentTimestamp + (item.duration - viewOffset)) }
				case _:
					pass
		if not self.discordIpcService.connected:
			self.discordIpcService.connect()
		if self.discordIpcService.connected:
			self.discordIpcService.setActivity(activity)
