"""Character Assets — Alliance Auth-plugin om assets terug te vinden.

Zoekt door de assets van alle gekoppelde characters heen, ook wat er *in* iets
anders ligt: in de cargo van een schip, in de Fleet Hangar of Ship Hangar van
een Bustard, in een container in een container, of in Asset Safety.

ESI geeft assets als een platte lijst waarin bijna alles in iets anders zit
(van 3326 assets van een character hadden er 3229 een kist of schip als
locatie). Zonder de keten omhoog te lopen lijkt vrijwel niets ergens te liggen.
Die keten is dan ook het hart van deze plugin.
"""

__version__ = "1.1.1"
__title__ = "Character Assets"
