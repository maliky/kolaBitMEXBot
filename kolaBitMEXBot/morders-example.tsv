name	tps_run	pause	essais	side	oType	tType	sDelta	tOut	atype	q	tp	prix	hook

# Acheter si ça monte (en limite)
Bul0	0 5440	1	10000	buy	SL	Si	1	18	qAt%p%	200	.7	.7 .8
Bul1	0 5440	1	10000	buy	SL	Si	1	20	qAt%p%	301	4	1 1.1

# Acheter si ça descend (en limite)
Bul2	0 5440	1	1	buy	L	Si	1		qAt%pD	202	8	-12 0
Bul3	0 5440	1	1	buy	L	Si	1		qAt%pD	203	8	-62 -50
Bul5	0 5440	1	1	buy	L	Si	1		qAt%pD	205	8	-102 -80

# Mettre un stop pour la position de 444
Stop4	0 5440	1	1	buy	L-!	Si-	1		qAt%pD	444	8	-1 1


# Sell puis buy chute brusquement
# Stop Buy
BearT6	0 5440	1	10000	sell	SL	S	5	15	qAtDpD	206	90	-172 -150
BearT7	0 5440	1	10000	sell	SL	S	10	18	qAtDpD	257	75	-222 -200
BearT8	0 5440	1	10000	sell	SLi	Si	1	19	qAtDpD	308	50	-282 -260
BearT9	0 5440	1	10000	sell	Si	Si	1	25	qAtDpD	789	130	-1262 -1240


# #### Notes ####
# oType = order price Type
# tType = tail Price Type
# Differents values des Types

# i, l, m (index, Last et Mark trigger Prices)  # par défaut last price which seem to be ask and bid prices

# M, L, S, MT, SL, LT: Market, Limit, Stop, MarketIfTouched, StopLimit, LimitIfTouched orders
# L toucher par rapport à LastPrice ?
# 
# !, - ParticipateDoNotInitiate et ReduceOnly, incompatible avec Market orders
# utiliser les SL et LT avec des limites juste à côte du trigger par défaut et dans le même sens
# tOut et pause sont en minutes

# sDelta (en diff nominal pour l'ordre principal seulement)  par défaut = 1  pour le stop

# sell SL, trigger when mark price < price given et le limit sera sell aussi

# temps: 24h 1440m, 48h 2880m,  4j 5760m, 7j ~10000m.

