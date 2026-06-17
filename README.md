<p align="center" > <img src="./MorphoDepot.png" width=300> </p>



# MorphoDepot
A distributed, github based platform to share and collaborate on segmentation using open-source 3D Slicer biomedical image computing platform. 

🌐 **Project website:** [morphodepot.org](https://morphodepot.org) &nbsp;·&nbsp; 📄 **Paper:** [Maga et al. 2026, *Methods in Ecology and Evolution*](https://doi.org/10.1111/2041-210x.70342)

The primary goal is to use github infrastructure to manage multi-person segmentation projects, particularly in context of classroom assignments.  A repository is used to manage segmentation of a specimen (e.g. a microCT of a fish) and issues are assigned to people to work on parts of the segmentation.  Pull requests are used to manage review and integration of segmentation tasks.

For background on the design and rationale, see our paper in *Methods in Ecology and Evolution*: [Forking anatomy: How MorphoDepot applies the open-source development model to 3D digital morphology](https://doi.org/10.1111/2041-210x.70342). The project website [morphodepot.org](https://morphodepot.org) collects documentation, events, and onboarding. Lab onboarding into the MorphoDepot GitHub organization ([join.morphodepot.org](https://join.morphodepot.org)) is in development and not yet open to the public; contributing segmentations to existing public repositories does not require onboarding.

The Slicer extension uses git behind the scenes, but most of the project management is done from within Slicer.

## Module Description
MorphoDepot consisted of a single module with a tabbed workflow

* **Configure:** contains variables such as github username, path to the GH cli and git, as well as where the local repositories will be stored. User cannot proceed unless all fields are configured correctly and they logged into GitHub via the GH cli commands (see next section).
* **Search:** Queries the github for currently existing MorphoDepot repositories and creates a summary table  Results can be filtered.
* **Annotate:** Primary interface the "segmentor" persona will interact with MorphoDepot. 
* **Review:** Primary interface the "repository owner" persona will interact with MorphoDepot
* **Create:** Interface to create a MorphoDepot repository from scratch
* **Release:** Interface for the "repository owner" persona to cut versioned releases (`v1`, `v2`, …) of a project. The owner selects the merged baseline segmentation and a color table, optionally attaches screenshots and release notes, and publishes a tagged GitHub release whose baseline becomes the starting point contributors sync to. The tab can also announce an upcoming release with a deadline to notify contributors and close out open issues before the release is cut.

<img src="./Search_MD.png" width="400">

## Prerequisites for using MorphoDepot
(If you using [MorphoCloud On Demand Instances](https://morphocloud.org), you only need to complete step #4)

1. Install [git command line tools for your operating system](https://git-scm.com/downloads). 
2. Install [GitHub CLI for your operating system.](https://cli.github.com/). 
3. Register an account on GitHub.com, if you don't already have one. 
4. On the computer you are planning to use the MorphoDepot, login to GitHub from the **terminal window** by giving this command: `gh auth login -s user:email`. (The `-s user:email` flag lets the MorphoDepot extension read your registered email — even when it is kept private on your profile — and auto-populate it for you, instead of asking you to type it in.) Make sure you follow the instructions all the way through, which involves the user pasting an 8-digit code (XXXX-YYYY) into the browser window, and authorizing GitHub. Make sure you have seen **"Congratulations, you're all set!"** in your browser window (if not, you need to repeat the steps).
5. Install 3D Slicer and MorphoDepot extension. You are now ready to use it. 

## MorphoDepot Personas
MorphoDepot workflows have two separate personas: **Repository Owner**, and **Individual (aka segmentor or contributor)**. 

* **Repository owners** create the MorphoDepot repositories which consists of the scan data, the specific anatomical terminology to be used to describe the anatomical contents, and optionally a segmentation. They need to complete the questionnaire about the specifics of the scan and specimen.  
* **Individuals** can be be anyone (e.g., students in a class, or a project team from a PI's lab, or an enthusiast on internet). If they want to contribute to the project, or complete a class assignment, they need to open an issue on the specific repository they want to work on and get assigned to it by the Repository Owner. Then they can complete these assignments via MorphoDepot module in 3D Slicer.

## MorphoDepot Step-by-Step Tutorial
For more information about how to create and operate MorphoDepot repositories, please see our [step-by-step tutorial](https://github.com/SlicerMorph/Tutorials/tree/main/MorphoDepot)

## Citation
If you use MorphoDepot in your research, please cite:

> Maga, A. M., Pieper, S., Donatelli, C., Gignac, P. M., Kolmann, M., Noto, C., Summers, A., & Taft, N. (2026). Forking anatomy: How MorphoDepot applies the open-source development model to 3D digital morphology. *Methods in Ecology and Evolution.* https://doi.org/10.1111/2041-210x.70342

## Funding 

MorphoDepot module is supported by funding from National Science Foundation ([DBI/2301405](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2301405&HistoricalAwards=false)). 
