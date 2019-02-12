#!/usr/bin/env python

"""
BINDetect_functions: Functions to be called from main BINDetect script

@author: Mette Bentsen
@contact: mette.bentsen (at) mpi-bn.mpg.de
@license: MIT

"""

import numpy as np
import pandas as pd
import scipy
from datetime import datetime
import itertools
import xlsxwriter
import random

#Plotting
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import NullFormatter
from cycler import cycler
from matplotlib.lines import Line2D
from adjustText import adjust_text

#Bio-specific packages
import pyBigWig
import pysam
import MOODS.scan
import MOODS.tools
import MOODS.parsers

#Internal functions and classes
from tobias.utils.regions import *
from tobias.utils.sequences import *
from tobias.utils.utilities import *
from tobias.utils.motifs import *
from tobias.utils.signals import *
from tobias.plotting.plot_bindetect import *

#------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------#

def get_gc_content(regions, fasta):
	""" Get GC content from regions in fasta """
	nuc_count = {"T":0, "t":0, "A":0, "a":0, "G":1, "g":1, "C":1, "c":1}

	gc = 0
	total = 0
	fasta_obj = pysam.FastaFile(fasta)
	for region in regions:
		seq = fasta_obj.fetch(region.chrom, region.start, region.end)
		gc += sum([nuc_count.get(nuc, 0.5) for nuc in seq])
		total += region.end - region.start
	fasta_obj.close()
	gc_content = gc / float(total)

	return(gc_content)


#---------------------------------------------------------------------------------------------------------#
#------------------------------------------- Main functions ----------------------------------------------#
#---------------------------------------------------------------------------------------------------------#

def scan_and_score(regions, motifs_obj, args, log_q, qs):
	""" Scanning and scoring runs in parallel for subsets of regions """
	
	logger = TobiasLogger("", args.verbosity, log_q)	#sending all logger calls to log_q

	logger.debug("Setting up scanner/bigwigs/fasta")
	motifs_obj.setup_moods_scanner()	#MotifList object

	pybw = {condition:pyBigWig.open(args.signals[i], "rb") for i, condition in enumerate(args.cond_names)}
	fasta_obj = pysam.FastaFile(args.genome)
	chrom_boundaries = dict(zip(fasta_obj.references, fasta_obj.lengths))

	gc_window = 500
	rand_window = 500
	extend = int(np.ceil(gc_window / 2.0))

	background_signal = {"gc":[], "signal":{condition:[] for condition in args.cond_names}}

	#TODO: Estimate number of background positions sampled to pre-allocate space

	######## Scan for motifs in each region ######
	logger.debug("Scanning for motif occurrences")
	all_TFBS = {TF: RegionList() for TF in motifs_obj.names} 	# Dict for saving sites before writing
	for i, region in enumerate(regions):
		logger.spam("Processing region: {0}".format(region.tup()))
	
		extra_columns = region

		#Random positions for sampling
		reglen = region.get_length()
		random.seed(reglen)		#Each region is processed identifically regardless of order in file
		rand_positions = random.sample(range(reglen), max(1,int(reglen/rand_window)))		#theoretically one in every 500 bp
		logger.spam("Random indices: {0} for region length {1}".format(rand_positions, reglen))

		#Read footprints in region
		footprints = {}
		for condition in args.cond_names:
			try:
				footprints[condition] = pybw[condition].values(region.chrom, region.start, region.end, numpy=True)
				footprints[condition] = np.nan_to_num(footprints[condition])	#nan to 0
			except:
				logger.error("Error reading footprints from region: {0}".format(region))
				continue

			#Read random positions for background
			for pos in rand_positions:
				background_signal["signal"][condition].append(footprints[condition][pos])

		#Scan for motifs across sequence from fasta
		extended_region = copy.copy(region).extend_reg(extend)	 #extend to calculate gc
		extended_region.check_boundary(chrom_boundaries, action="cut")

		seq = fasta_obj.fetch(region.chrom, extended_region.start, extended_region.end)

		#Calculate GC content for regions
		num_sequence = nuc_to_num(seq) 
		Ns = num_sequence == 4
		boolean = 1 * (num_sequence > 1)		# Convert to 0/1 gc
		boolean[Ns] = 0.5						# replace Ns 0.5 - with neither GC nor AT
		boolean = boolean.astype(np.float64)	# due to input of fast_rolling_math
		gc = fast_rolling_math(boolean, gc_window, "mean")
		gc = gc[extend:-extend]
		background_signal["gc"].extend([gc[pos] for pos in rand_positions])

		region_TFBS = motifs_obj.scan_sequence(seq[extend:-extend], region)		#RegionList of TFBS

		#Extend all TFBS with extra columns from peaks and bigwigs 
		extra_columns = region
		for TFBS in region_TFBS:
			motif_length = TFBS.end - TFBS.start 
			pos = TFBS.start - region.start + int(motif_length/2.0) #middle of TFBS
			
			TFBS.extend(extra_columns)
			TFBS.append(gc[pos])

			#Assign scores from bigwig
			for bigwig in args.cond_names:
				bigwig_score = footprints[bigwig][pos]
				TFBS.append("{0:.5f}".format(bigwig_score))

		#Split regions to single TFs
		for TFBS in region_TFBS:
			all_TFBS[TFBS.name].append(TFBS)

	####### All input regions have been scanned #######
	global_TFBS = RegionList()	#across all TFs

	#Sent sites to writer
	for name in all_TFBS:	
		all_TFBS[name] = all_TFBS[name].resolve_overlaps()
		no_sites = len(all_TFBS[name])

		logger.spam("Sending {0} sites from {1} to bed-writer queue".format(no_sites, name))
		bed_content = all_TFBS[name].as_bed()	#string 
		qs[name].put((name, bed_content))

		global_TFBS.extend(all_TFBS[name])
		all_TFBS[name] = []

	overlap = global_TFBS.count_overlaps()

	#Close down open file handles
	fasta_obj.close()
	for bigwig_f in pybw:
		pybw[bigwig_f].close()
			
	logger.stop()
	logger.total_time

	return(background_signal, overlap)


#-----------------------------------------------------------------------------------------------#
def process_tfbs(TF_name, args, log2fc_params): 	#per tf
	""" Processes single TFBS to split into bound/unbound and write out overview file """

	begin_time = datetime.now()
	logger = TobiasLogger("", args.verbosity, args.log_q) 	#sending all logger calls to log_q

	#pre-scanned sites to read
	bed_outdir = os.path.join(args.outdir, TF_name, "beds")
	filename = os.path.join(bed_outdir, TF_name + ".tmp")
	no_cond = len(args.cond_names)
	comparisons = args.comparisons

	#Get info table ready
	info_columns = ["total_tfbs"]
	info_columns.extend(["{0}_{1}".format(cond, metric) for (cond, metric) in itertools.product(args.cond_names, ["mean_score", "bound"])])
	info_columns.extend(["{0}_{1}_{2}".format(comparison[0], comparison[1], metric) for (comparison, metric) in itertools.product(comparisons, ["change", "pvalue"])])
	rows, cols = 1, len(info_columns)
	info_table = pd.DataFrame(np.zeros((rows, cols)), columns=info_columns, index=[TF_name])

	#Read file to pandas
	arr = np.genfromtxt(filename, dtype=None, delimiter="\t", names=None, encoding="utf8", autostrip=True)	#Read using genfromtxt to get automatic type
	bed_table = pd.DataFrame(arr, index=None, columns=None)
	no_rows, no_cols = bed_table.shape

	#no_rows, no_cols = overview_table.shape
	info_table.at[TF_name, "total_tfbs"] = no_rows

	#Set header in overview
	header = [""]*no_cols
	header[:6] = ["TFBS_chr", "TFBS_start", "TFBS_end", "TFBS_name", "TFBS_score", "TFBS_strand"]
	
	if args.peak_header_list != None:
		header[6:6+len(args.peak_header_list)] = args.peak_header_list
	else:
		no_peak_col = len(header[6:])
		header[6:6+no_peak_col] = ["peak_chr", "peak_start", "peak_end"] + ["additional_" + str(num + 1) for num in range(no_peak_col-3)]

	header[-no_cond:] = ["{0}_score".format(condition) for condition in args.cond_names] 	#signal scores
	header[-no_cond-1] = "GC"
	bed_table.columns = header

	#Sort and format
	bed_table = bed_table.sort_values(["TFBS_chr", "TFBS_start", "TFBS_end"])
	for condition in args.cond_names:
		bed_table[condition + "_score"] = bed_table[condition + "_score"].round(5)

	#### Write _all file ####
	chosen_columns = [col for col in header if col != "GC"]
	outfile = os.path.join(bed_outdir, TF_name + "_all.bed")
	bed_table.to_csv(outfile, sep="\t", index=False, header=False, columns=chosen_columns)

	#### Estimate bound/unbound split ####
	for condition in args.cond_names:

		threshold = args.thresholds[condition]
		bed_table[condition + "_bound"] = np.where(bed_table[condition + "_score"] > threshold, 1, 0).astype(int)
		
		info_table.at[TF_name, condition + "_mean_score"] = round(np.mean(bed_table[condition + "_score"]), 5)
		info_table.at[TF_name, condition + "_bound"] = np.sum(bed_table[condition + "_bound"].values)	#_bound contains bool 0/1

	#Write bound/unbound
	for (condition, state) in itertools.product(args.cond_names, ["bound", "unbound"]):

		outfile = os.path.join(bed_outdir, "{0}_{1}_{2}.bed".format(TF_name, condition, state))

		#Subset bed table
		chosen_bool = 1 if state == "bound" else 0
		bed_table_subset = bed_table.loc[bed_table[condition + "_bound"] == chosen_bool]
		bed_table_subset = bed_table_subset.sort_values([condition + "_score"], ascending=False)

		#Write out subset with subset of columns
		chosen_columns = header[:-no_cond-1] + [condition + "_score"]
		bed_table_subset.to_csv(outfile, sep="\t", index=False, header=False, columns=chosen_columns)


	#### Calculate statistical test in comparison to background ####
	fig_out = os.path.abspath(os.path.join(args.outdir, TF_name, "plots", TF_name + "_log2fcs.pdf"))
	log2fc_pdf = PdfPages(fig_out, keep_empty=True)

	for i, (cond1, cond2) in enumerate(comparisons):
		base = "{0}_{1}".format(cond1, cond2)

		#Calculate log2fcs of TFBS for this TF
		cond1_values = bed_table[cond1 + "_score"].values
		cond2_values = bed_table[cond2 + "_score"].values
		bed_table[base + "_log2fc"] = np.log2(np.true_divide(cond1_values + args.pseudo, cond2_values + args.pseudo))
		bed_table[base + "_log2fc"] = bed_table[base + "_log2fc"].round(5)
		
		# Compare log2fcs to background log2fcs
		included = np.logical_or(bed_table[cond1 + "_score"].values > 0, bed_table[cond2 + "_score"].values > 0)
		subset = bed_table[included].copy() 		#included subset 
		subset.loc[:,"peak_id"] = ["_".join([chrom, str(start), str(end)]) for (chrom, start, end) in zip(subset["peak_chr"].values, subset["peak_start"].values, subset["peak_end"].values)]	
		
		observed_log2fcs = subset.groupby('peak_id')[base + '_log2fc'].mean().reset_index()[base + "_log2fc"].values		#if more than one TFBS per peak -> take mean value

		#Estimate mean/std
		bg_params = log2fc_params[(cond1, cond2)]
		obs_params = scipy.stats.norm.fit(observed_log2fcs)

		obs_mean, obs_std = obs_params
		bg_mean, bg_std = bg_params
		obs_no = np.min([len(observed_log2fcs), 50000])		#Set cap on obs_no to prevent super small p-values

		#If there was any change found at all (0 can happen if two bigwigs are the same)
		if obs_mean != bg_mean: 
			info_table.at[TF_name, base + "_change"] = (obs_mean - bg_mean) / np.mean([obs_std, bg_std])  #effect size
			info_table.at[TF_name, base + "_change"] = np.round(info_table.at[TF_name, base + "_change"], 5)
		
			#pval = scipy.stats.mannwhitneyu(observed_log2fcs, bg_log2fcs, alternative="two-sided")[1]
			pval = scipy.stats.ttest_ind_from_stats(obs_mean, obs_std, obs_no, bg_mean, bg_std, obs_no, equal_var=False)[1] 	#pvalue is second in tup
			info_table.at[TF_name, base + "_pvalue"] = pval
		
		#Else not possible to compare groups
		else:
			info_table.at[TF_name, base + "_change"] = 0
			info_table.at[TF_name, base + "_pvalue"] = 1

		#### Plot comparison ###
		fig, ax = plt.subplots(1,1)
		ax.hist(observed_log2fcs, bins='auto', label="Observed log2fcs", density=True)
		xvals = np.linspace(plt.xlim()[0], plt.xlim()[1], 100)
		
		#Observed distribution
		pdf = scipy.stats.norm.pdf(xvals, *obs_params)
		ax.plot(xvals, pdf, label="Observed distribution (fit)", color="red", linestyle="--")
		ax.axvline(obs_mean, color="red", label="Observed mean")
		
		#Background distribution
		pdf = scipy.stats.norm.pdf(xvals, *bg_params)
		ax.plot(xvals, pdf, label="Background distribution (fit)", color="Black", linestyle="--")
		ax.axvline(bg_mean, color="black", label="Background mean")

		#Set size
		x0,x1 = ax.get_xlim()
		y0,y1 = ax.get_ylim()
		ax.set_aspect(((x1-x0)/(y1-y0)) / 1.5)

		#Decorate
		ax.legend()
		plt.xlabel("Log2 fold change")
		plt.ylabel("Density")
		plt.title("Differential binding for TF \"{0}\"\nbetween ({1} / {2})".format(TF_name, cond1, cond2))
		ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
		
		plt.tight_layout()
		log2fc_pdf.savefig(fig, bbox_inches='tight')
		plt.close(fig)

	log2fc_pdf.close()	


	########## Write overview with scores, bound and log2fcs ##############
	chosen_columns = [col for col in bed_table.columns if col != "GC"]
	overview_txt = os.path.join(args.outdir, TF_name, TF_name + "_overview.txt")
	bed_table.to_csv(overview_txt, sep="\t", index=False, header=True, columns=chosen_columns)

	#Write xlsx overview
	try:
		overview_excel = os.path.join(args.outdir, TF_name, TF_name + "_overview.xlsx")
		writer = pd.ExcelWriter(overview_excel, engine='xlsxwriter')
		bed_table.to_excel(writer, index=False, columns=chosen_columns)
		
		worksheet = writer.sheets['Sheet1']
		no_rows, no_cols = bed_table.shape
		worksheet.autofilter(0,0,no_rows, no_cols-1)
		writer.save()

	except:
		print("Error writing excelfile for TF {0}".format(TF_name))
		sys.exit() #logger.critical("Error writing excelfile for TF {0}".format(TF_name)
	
	#### Remove temporary file ####
	try:
		os.remove(filename)
	except:
		logger.error("Could not remove temporary file {0} - this does not effect the results of BINDetect.".format(filename) )

	return(info_table)



#------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------#

def plot_bindetect(motifs, clusters, conditions, args):
	""" Conditions refer to the order of the fold_change divison, meaning condition1/condition2 
		- Clusters is a RegionCluster object 
		- conditions is a tup of condition names (cond1, cond2)
	"""
	warnings.filterwarnings("ignore")

	cond1, cond2 = conditions
	no_IDS = clusters.n

	#Link information from motifs / clusters
	diff_scores = {}
	for motif in motifs:
		diff_scores[motif.name] = {"change": motif.change,
									"pvalue": motif.pvalue,
									"log10pvalue": -np.log10(motif.pvalue) if  motif.pvalue > 0 else -np.log10(1e-308),	#smallest possible number before python underflows
									"volcano_label": motif.alt_name,	#shorter name
									"overview_label": "{0} ({1})".format(motif.alt_name, motif.id) 		#the name which was output used in bindetect output
									}
	
	xvalues = np.array([diff_scores[TF]["change"] for TF in diff_scores])
	yvalues = np.array([diff_scores[TF]["log10pvalue"] for TF in diff_scores])

	#### Define the TFs to plot IDs for ####
	y_min = np.percentile(yvalues[yvalues < -np.log10(1e-300)], 95)	
	x_min, x_max = np.percentile(xvalues, [5,95])

	for TF in diff_scores:
		if diff_scores[TF]["change"] < x_min or diff_scores[TF]["change"] > x_max or diff_scores[TF]["log10pvalue"] > y_min:
			diff_scores[TF]["show"] = True
			if diff_scores[TF]["change"] < 0:
				diff_scores[TF]["color"] = "blue"
			elif diff_scores[TF]["change"] > 0:
				diff_scores[TF]["color"] = "red"
		else:
			diff_scores[TF]["show"] = False
			diff_scores[TF]["color"] = "black"

	node_color = clusters.node_color
	IDS = np.array(clusters.names)
	
	#--------------------------------------- Figure --------------------------------#

	#Make figure
	no_rows, no_cols = 2,2	
	h_ratios = [1,max(1,no_IDS/25)]
	figsize = (8,10+7*(no_IDS/25))
	
	fig = plt.figure(figsize = figsize)
	gs = gridspec.GridSpec(no_rows, no_cols, height_ratios=h_ratios)
	gs.update(hspace=0.0001, bottom=0.00001, top=0.999999)

	ax1 = fig.add_subplot(gs[0,:])	#volcano
	ax2 = fig.add_subplot(gs[1,0])	#long scatter overview
	ax3 = fig.add_subplot(gs[1,1])  #dendrogram
	
	######### Volcano plot on top of differential values ########
	ax1.set_title("BINDetect volcano plot", fontsize=16, pad=20)
	ax1.scatter(xvalues, yvalues, color="black", s=5)

	#Add +/- 10% to make room for labels
	ylim = ax1.get_ylim()
	y_extra = (ylim[1] - ylim[0]) * 0.1
	ax1.set_ylim(ylim[0], ylim[1] + y_extra)

	xlim = ax1.get_xlim()
	x_extra = (xlim[1] - xlim[0]) * 0.1
	lim = np.max([np.abs(xlim[0]-x_extra), np.abs(xlim[1]+x_extra)])
	ax1.set_xlim(-lim, lim)

	x0,x1 = ax1.get_xlim()
	y0,y1 = ax1.get_ylim()
	ax1.set_aspect((x1-x0)/(y1-y0))		#square volcano plot

	#Decorate plot
	ax1.set_xlabel("Differential binding score")
	ax1.set_ylabel("-log10(pvalue)")

	########### Dendrogram over similarities of TFs #######
	dendro_dat = dendrogram(clusters.linkage_mat, labels=IDS, no_labels=True, orientation="right", ax=ax3, above_threshold_color="black", link_color_func=lambda k: clusters.node_color[k])
	labels = dendro_dat["ivl"]	#Now sorted for the order in dendrogram
	ax3.set_xlabel("Transcription factor similarities\n(Clusters below threshold are colored)")

	ax3.set_ylabel("Transcription factor clustering based on TFBS overlap", rotation=270, labelpad=20)
	ax3.yaxis.set_label_position("right")

	#Set aspect of dendrogram/changes
	x0,x1 = ax3.get_xlim()
	y0,y1 = ax3.get_ylim()
	ax3.set_aspect(((x1-x0)/(y1-y0)) * no_IDS/10)		

	########## Differential binding scores per TF ##########
	ax2.set_xlabel("Differential binding score\n" + "(" + cond2 + r' $\leftarrow$' + r'$\rightarrow$ ' + cond1 + ")") #First position in comparison equals numerator in log2fc division
	ax2.xaxis.set_label_position('bottom') 
	ax2.xaxis.set_ticks_position('bottom') 

	no_labels = len(labels)
	ax2.set_ylim(0.5, no_labels+0.5)
	ax2.set_ylabel("Transcription factors")

	ax2.set_yticks(range(1,no_labels+1))
	ax2.set_yticklabels([diff_scores[TF]["overview_label"] for TF in labels])
	ax2.axvline(0, color="grey", linestyle="--") 	#Plot line at middle

	#Plot scores per TF
	for y, TF in enumerate(labels):	#labels are the output motif names from output
		

		idx = np.where(IDS == TF)[0][0]
		score = diff_scores[TF]["change"]

		#Set coloring based on change/pvalue
		if diff_scores[TF]["show"] == True:
			fill = "full"
		else:
			fill = "none"

		ax2.axhline(y+1, color="grey", linewidth=1)
		ax2.plot(score, y+1, marker='o', color=node_color[idx], fillstyle=fill)
		ax2.yaxis.get_ticklabels()[y].set_color(node_color[idx])

	#Set x-axis ranges
	lim = np.max(np.abs(ax2.get_xlim()))
	ax2.set_xlim((-lim, lim))	#center on 0

	#set aspect
	x0,x1 = ax2.get_xlim()
	y0,y1 = ax2.get_ylim()
	ax2.set_aspect(((x1-x0)/(y1-y0)) * no_IDS/10)		#square volcano plot

	plt.tight_layout()    #tight layout before setting ids in volcano plot

	######### Color points and set labels in volcano ########
	txts = []
	for TF in diff_scores:
		coord = [diff_scores[TF]["change"], diff_scores[TF]["log10pvalue"]]
		ax1.scatter(coord[0], coord[1], color=diff_scores[TF]["color"], s=4.5)

		if diff_scores[TF]["show"] == True:
			txts.append(ax1.text(coord[0], coord[1], diff_scores[TF]["volcano_label"], fontsize=7))

	adjust_text(txts, ax=ax1, text_from_points=True, arrowprops=dict(arrowstyle='-', color='black', lw=0.5))  #, expand_text=(0.1,1.2), expand_objects=(0.1,0.1))
	
	#Plot custom legend for colors
	legend_elements = [Line2D([0],[0], marker='o', color='w', markerfacecolor="red", label="More bound in {0}".format(conditions[0])),
						Line2D([0],[0], marker='o', color='w', markerfacecolor="blue", label="More bound in {0}".format(conditions[1]))]
	ax1.legend(handles=legend_elements, bbox_to_anchor=(1.05, 1), loc='upper left')

	return(fig)
